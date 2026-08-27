"""Local tools exposed to the model through native function calling."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from coding_agent.errors import ToolError


_IGNORED_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache"}
_PROTECTED_FILENAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}
_DANGEROUS_COMMAND_PATTERNS = (
    r"(^|\s)rm\s+(-[^\s]*r[^\s]*f|-[^\s]*f[^\s]*r)\b",
    r"(^|\s)git\s+reset\s+--hard\b",
    r"(^|\s)git\s+clean\s+-[^\s]*f",
    r"(^|\s)(shutdown|reboot|halt|format)(\.exe)?\b",
    r"(^|\s)(del|erase)\s+[^\r\n]*(/s|/q)",
    r"(^|\s)rmdir\s+[^\r\n]*/s\b",
    r"\bremove-item\b[^\r\n]*\b-recurse\b",
)
_SENSITIVE_ENV_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PRIVATE_KEY")


class WorkspaceTools:
    """Tool registry whose file operations stay inside one workspace."""

    def __init__(
        self,
        root: str | Path,
        *,
        output_limit: int = 20_000,
        command_timeout: int = 120,
    ) -> None:
        resolved_root = Path(root).expanduser().resolve()
        if not resolved_root.is_dir():
            raise ToolError(f"工作区不存在或不是目录: {resolved_root}")
        self.root = resolved_root
        self.output_limit = output_limit
        self.command_timeout = command_timeout
        self._handlers: dict[str, Callable[..., str]] = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "replace_in_file": self.replace_in_file,
            "search_text": self.search_text,
            "run_command": self.run_command,
        }

    def schemas(self) -> list[dict[str, Any]]:
        return [
            self._schema(
                "list_files",
                "List files in the workspace. Use this before guessing paths.",
                {
                    "path": {"type": "string", "description": "Workspace-relative directory; default is ."},
                    "recursive": {"type": "boolean", "description": "Recurse into subdirectories"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
            ),
            self._schema(
                "read_file",
                "Read a UTF-8 text file with line numbers.",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                required=["path"],
            ),
            self._schema(
                "write_file",
                "Create or fully overwrite a UTF-8 text file. Parent directories are created.",
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                required=["path", "content"],
            ),
            self._schema(
                "replace_in_file",
                "Replace exact text in a UTF-8 file; fails unless the occurrence count matches.",
                {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                required=["path", "old", "new"],
            ),
            self._schema(
                "search_text",
                "Search text files and return matching workspace-relative paths and line numbers.",
                {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "file_glob": {"type": "string", "description": "Example: *.py"},
                    "case_sensitive": {"type": "boolean"},
                    "regex": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                required=["query"],
            ),
            self._schema(
                "run_command",
                "Run a non-interactive shell command in the workspace. Credentials are removed from its environment.",
                {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                required=["command"],
            ),
        ]

    @staticmethod
    def _schema(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, name: str, arguments: Any) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return self._result(False, error=f"未知工具: {name}")
        if not isinstance(arguments, dict):
            return self._result(False, error="工具参数必须是 JSON 对象。")
        try:
            output = handler(**arguments)
            return self._result(True, output=self._truncate(output))
        except TypeError as exc:
            return self._result(False, error=f"工具参数无效: {exc}")
        except (ToolError, OSError, ValueError, re.error) as exc:
            return self._result(False, error=str(exc))

    @staticmethod
    def _result(ok: bool, **payload: Any) -> str:
        return json.dumps({"ok": ok, **payload}, ensure_ascii=False)

    def _truncate(self, text: str) -> str:
        if len(text) <= self.output_limit:
            return text
        omitted = len(text) - self.output_limit
        return f"{text[:self.output_limit]}\n...[工具输出截断 {omitted} 个字符]"

    def _resolve(self, requested_path: str, *, allow_missing: bool = False) -> Path:
        if not isinstance(requested_path, str) or not requested_path.strip():
            raise ToolError("path 必须是非空字符串。")
        candidate = Path(requested_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("拒绝访问工作区之外的路径。") from exc

        relative_parts = candidate.relative_to(self.root).parts
        if any(part.lower() == ".git" for part in relative_parts):
            raise ToolError("拒绝通过智能体工具直接访问 .git 元数据。")
        if self._is_protected_filename(candidate.name):
            raise ToolError(f"拒绝访问潜在凭据文件: {candidate.name}")
        if not allow_missing and not candidate.exists():
            raise ToolError(f"路径不存在: {self._relative(candidate)}")
        return candidate

    def _relative(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        return "." if not relative.parts else relative.as_posix()

    def list_files(
        self,
        path: str = ".",
        recursive: bool = True,
        max_results: int = 200,
    ) -> str:
        directory = self._resolve(path)
        if not directory.is_dir():
            raise ToolError(f"不是目录: {self._relative(directory)}")
        max_results = self._bounded_int(max_results, "max_results", 1, 1000)
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        results: list[str] = []
        for item in iterator:
            try:
                relative = item.relative_to(self.root)
            except ValueError:
                continue
            if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
                continue
            if item.is_file():
                results.append(relative.as_posix())
                if len(results) >= max_results:
                    break
        results.sort()
        suffix = "\n[结果已达到上限]" if len(results) >= max_results else ""
        return "\n".join(results) + suffix if results else "[没有文件]"

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> str:
        file_path = self._resolve(path)
        if not file_path.is_file():
            raise ToolError(f"不是文件: {self._relative(file_path)}")
        start_line = self._bounded_int(start_line, "start_line", 1, 10_000_000)
        if end_line is not None:
            end_line = self._bounded_int(end_line, "end_line", start_line, 10_000_000)
        if file_path.stat().st_size > 5_000_000:
            raise ToolError("文件超过 5 MB；请使用搜索或读取更小的文件。")
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ToolError("文件不是 UTF-8 文本，可能是二进制文件。") from exc
        stop = end_line if end_line is not None else len(lines)
        selected = lines[start_line - 1 : stop]
        if not selected and start_line > len(lines):
            return f"[文件共 {len(lines)} 行，请求范围为空]"
        return "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=start_line)
        )

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串。")
        if len(content) > 5_000_000:
            raise ToolError("单次写入不能超过 5,000,000 个字符。")
        file_path = self._resolve(path, allow_missing=True)
        if file_path.exists() and file_path.is_dir():
            raise ToolError(f"目标是目录: {self._relative(file_path)}")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(file_path, content)
        return f"已写入 {self._relative(file_path)}（{len(content)} 个字符）。"

    def replace_in_file(
        self,
        path: str,
        old: str,
        new: str,
        expected_occurrences: int = 1,
    ) -> str:
        if not isinstance(old, str) or not old:
            raise ToolError("old 必须是非空字符串。")
        if not isinstance(new, str):
            raise ToolError("new 必须是字符串。")
        expected_occurrences = self._bounded_int(
            expected_occurrences, "expected_occurrences", 1, 1000
        )
        file_path = self._resolve(path)
        if not file_path.is_file():
            raise ToolError(f"不是文件: {self._relative(file_path)}")
        try:
            original = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("文件不是 UTF-8 文本，无法替换。") from exc
        occurrences = original.count(old)
        if occurrences != expected_occurrences:
            raise ToolError(
                f"精确替换未执行：期望 {expected_occurrences} 处，实际 {occurrences} 处。"
            )
        updated = original.replace(old, new)
        self._atomic_write(file_path, updated)
        return f"已更新 {self._relative(file_path)}（替换 {occurrences} 处）。"

    def search_text(
        self,
        query: str,
        path: str = ".",
        file_glob: str = "*",
        case_sensitive: bool = False,
        regex: bool = False,
        max_results: int = 200,
    ) -> str:
        if not isinstance(query, str) or not query:
            raise ToolError("query 必须是非空字符串。")
        directory = self._resolve(path)
        if not directory.is_dir():
            raise ToolError(f"不是目录: {self._relative(directory)}")
        max_results = self._bounded_int(max_results, "max_results", 1, 1000)
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query if regex else re.escape(query), flags)
        results: list[str] = []
        scanned = 0
        for file_path in directory.rglob("*"):
            relative = file_path.relative_to(self.root)
            if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
                continue
            if not file_path.is_file() or not fnmatch.fnmatch(file_path.name, file_glob):
                continue
            if self._is_protected_filename(file_path.name):
                continue
            scanned += 1
            if scanned > 10_000:
                break
            try:
                if file_path.stat().st_size > 2_000_000:
                    continue
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    results.append(f"{relative.as_posix()}:{line_number}: {line}")
                    if len(results) >= max_results:
                        return "\n".join(results) + "\n[结果已达到上限]"
        return "\n".join(results) if results else "[没有匹配项]"

    def run_command(self, command: str, timeout_seconds: int | None = None) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command 必须是非空字符串。")
        lowered = command.lower()
        if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in _DANGEROUS_COMMAND_PATTERNS):
            raise ToolError("命令被安全策略拦截；请改用非破坏性的具体操作。")
        timeout = self.command_timeout if timeout_seconds is None else self._bounded_int(
            timeout_seconds, "timeout_seconds", 1, 600
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS)
        }
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                shell=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._stream_text(exc.stdout)
            stderr = self._stream_text(exc.stderr)
            raise ToolError(
                f"命令在 {timeout} 秒后超时。\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ) from exc
        return (
            f"exit_code: {completed.returncode}\n"
            f"stdout:\n{completed.stdout or '[空]'}\n"
            f"stderr:\n{completed.stderr or '[空]'}"
        )

    @staticmethod
    def _stream_text(value: str | bytes | None) -> str:
        if value is None:
            return "[空]"
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    @staticmethod
    def _is_protected_filename(filename: str) -> bool:
        lowered = filename.lower()
        return lowered in _PROTECTED_FILENAMES or lowered.startswith(".env.")

    @staticmethod
    def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{name} 必须是整数。")
        if not minimum <= value <= maximum:
            raise ToolError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
        return value

    @staticmethod
    def _atomic_write(file_path: Path, content: str) -> None:
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(content)
                temporary_name = stream.name
            os.replace(temporary_name, file_path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)
