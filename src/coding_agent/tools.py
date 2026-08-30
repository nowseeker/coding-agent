"""Local tools exposed to the model through native function calling."""

from __future__ import annotations

import fnmatch
import inspect
import json
import os
import re
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, get_args, get_type_hints

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
_SUPPORTED_JSON_TYPES = {"string": str, "integer": int, "boolean": bool}
_ALLOWED_PARAMETER_RULES = {
    "type",
    "description",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
}


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        return "object"
    return type(value).__name__


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Single source for a model-visible schema and its local handler."""

    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    handler: Callable[..., str]

    def validate_contract(self) -> None:
        """Fail fast when schema fields drift from the Python handler."""

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.name):
            raise ToolError(f"工具名称无效: {self.name!r}")
        if not self.description.strip():
            raise ToolError(f"工具 {self.name} 缺少说明。")

        signature = inspect.signature(self.handler)
        handler_parameters = signature.parameters
        unsupported_kinds = {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }
        unsupported = [
            parameter.name
            for parameter in handler_parameters.values()
            if parameter.kind in unsupported_kinds
        ]
        if unsupported:
            raise ToolError(
                f"工具 {self.name} 的处理函数包含不支持的参数形式: {unsupported}"
            )

        schema_names = set(self.parameters)
        handler_names = set(handler_parameters)
        if schema_names != handler_names:
            missing_schema = sorted(handler_names - schema_names)
            missing_handler = sorted(schema_names - handler_names)
            raise ToolError(
                f"工具 {self.name} 的 Schema 与处理函数参数不一致；"
                f"Schema 缺少 {missing_schema}，处理函数缺少 {missing_handler}。"
            )

        type_hints = get_type_hints(self.handler)
        for parameter_name, rules in self.parameters.items():
            if not isinstance(rules, dict):
                raise ToolError(
                    f"工具 {self.name} 参数 {parameter_name} 的 Schema 必须是对象。"
                )
            unknown_rules = sorted(set(rules) - _ALLOWED_PARAMETER_RULES)
            if unknown_rules:
                raise ToolError(
                    f"工具 {self.name} 参数 {parameter_name} 包含不支持的 Schema 规则: "
                    f"{unknown_rules}"
                )
            json_type = rules.get("type")
            python_type = _SUPPORTED_JSON_TYPES.get(json_type)
            if python_type is None:
                raise ToolError(
                    f"工具 {self.name} 参数 {parameter_name} 使用了不支持的类型: "
                    f"{json_type!r}"
                )
            annotation = type_hints.get(parameter_name)
            if annotation is None or not self._annotation_accepts(annotation, python_type):
                raise ToolError(
                    f"工具 {self.name} 参数 {parameter_name} 的 JSON 类型 {json_type} "
                    "与处理函数类型标注不一致。"
                )
            self._validate_rule_bounds(parameter_name, rules, json_type)
            default = handler_parameters[parameter_name].default
            if default is not inspect.Parameter.empty and default is not None:
                if type(default) is not python_type:
                    raise ToolError(
                        f"工具 {self.name} 参数 {parameter_name} 的默认值类型与 Schema 不一致。"
                    )
                default_issues = self._validate_value(parameter_name, default, rules)
                if default_issues:
                    raise ToolError(
                        f"工具 {self.name} 参数 {parameter_name} 的默认值不符合 Schema: "
                        f"{default_issues[0]['message']}"
                    )

    def schema(self) -> dict[str, Any]:
        properties = deepcopy(self.parameters)
        signature = inspect.signature(self.handler)
        required: list[str] = []
        for name, parameter in signature.parameters.items():
            if parameter.default is inspect.Parameter.empty:
                required.append(name)
            elif parameter.default is not None:
                properties[name].setdefault("default", parameter.default)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    def validate_arguments(self, arguments: Any) -> list[dict[str, Any]]:
        """Validate the supported JSON Schema subset without coercing values."""

        if not isinstance(arguments, dict):
            return [
                {
                    "field": "$",
                    "code": "type_mismatch",
                    "expected": "object",
                    "received": _json_type_name(arguments),
                    "message": "工具参数必须是 JSON 对象。",
                }
            ]

        issues: list[dict[str, Any]] = []
        signature = inspect.signature(self.handler)
        required = {
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
        }
        for name in sorted(required - set(arguments)):
            issues.append(
                {
                    "field": name,
                    "code": "required",
                    "message": f"缺少必填参数 {name}。",
                }
            )

        for name in arguments:
            if not isinstance(name, str) or name not in self.parameters:
                issues.append(
                    {
                        "field": str(name),
                        "code": "additional_property",
                        "message": f"不允许额外参数 {name!r}。",
                    }
                )

        for name, rules in self.parameters.items():
            if name not in arguments:
                continue
            value = arguments[name]
            expected_name = rules["type"]
            expected_type = _SUPPORTED_JSON_TYPES[expected_name]
            if type(value) is not expected_type:
                issues.append(
                    {
                        "field": name,
                        "code": "type_mismatch",
                        "expected": expected_name,
                        "received": _json_type_name(value),
                        "message": (
                            f"参数 {name} 必须是 {expected_name}，"
                            f"实际是 {_json_type_name(value)}。"
                        ),
                    }
                )
                continue
            issues.extend(self._validate_value(name, value, rules))
        return issues

    @staticmethod
    def _annotation_accepts(annotation: Any, expected: type[Any]) -> bool:
        if annotation is expected or annotation is Any:
            return True
        return expected in get_args(annotation)

    @staticmethod
    def _validate_rule_bounds(
        parameter_name: str,
        rules: dict[str, Any],
        json_type: str,
    ) -> None:
        if json_type == "integer":
            minimum = rules.get("minimum")
            maximum = rules.get("maximum")
            for label, value in (("minimum", minimum), ("maximum", maximum)):
                if value is not None and (type(value) is not int):
                    raise ToolError(f"参数 {parameter_name} 的 {label} 必须是整数。")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ToolError(f"参数 {parameter_name} 的 minimum 不能大于 maximum。")
        if json_type == "string":
            minimum = rules.get("minLength")
            maximum = rules.get("maxLength")
            for label, value in (("minLength", minimum), ("maxLength", maximum)):
                if value is not None and (type(value) is not int or value < 0):
                    raise ToolError(f"参数 {parameter_name} 的 {label} 必须是非负整数。")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ToolError(f"参数 {parameter_name} 的 minLength 不能大于 maxLength。")

    @staticmethod
    def _validate_value(
        name: str,
        value: Any,
        rules: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if type(value) is int:
            minimum = rules.get("minimum")
            maximum = rules.get("maximum")
            if minimum is not None and value < minimum:
                issues.append(
                    {
                        "field": name,
                        "code": "minimum",
                        "expected": f">= {minimum}",
                        "received": value,
                        "message": f"参数 {name} 不能小于 {minimum}。",
                    }
                )
            if maximum is not None and value > maximum:
                issues.append(
                    {
                        "field": name,
                        "code": "maximum",
                        "expected": f"<= {maximum}",
                        "received": value,
                        "message": f"参数 {name} 不能大于 {maximum}。",
                    }
                )
        if type(value) is str:
            minimum = rules.get("minLength")
            maximum = rules.get("maxLength")
            if minimum is not None and len(value) < minimum:
                issues.append(
                    {
                        "field": name,
                        "code": "min_length",
                        "expected": f"length >= {minimum}",
                        "received": len(value),
                        "message": f"参数 {name} 长度不能小于 {minimum}。",
                    }
                )
            if maximum is not None and len(value) > maximum:
                issues.append(
                    {
                        "field": name,
                        "code": "max_length",
                        "expected": f"length <= {maximum}",
                        "received": len(value),
                        "message": f"参数 {name} 长度不能大于 {maximum}。",
                    }
                )
        return issues


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
        specs = self._build_specs()
        names = [spec.name for spec in specs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ToolError(f"工具名称重复: {duplicates}")
        for spec in specs:
            spec.validate_contract()
        self._specs = {spec.name: spec for spec in specs}

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    def _build_specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                "list_files",
                "List files in the workspace. Use this before guessing paths.",
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Workspace-relative directory; default is .",
                    },
                    "recursive": {"type": "boolean", "description": "Recurse into subdirectories"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                self.list_files,
            ),
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file with line numbers.",
                {
                    "path": {"type": "string", "minLength": 1},
                    "start_line": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                    "end_line": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                },
                self.read_file,
            ),
            ToolSpec(
                "write_file",
                "Create or fully overwrite a UTF-8 text file. Parent directories are created.",
                {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string", "maxLength": 5_000_000},
                },
                self.write_file,
            ),
            ToolSpec(
                "replace_in_file",
                "Replace exact text in a UTF-8 file; fails unless the occurrence count matches.",
                {
                    "path": {"type": "string", "minLength": 1},
                    "old": {"type": "string", "minLength": 1},
                    "new": {"type": "string"},
                    "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                self.replace_in_file,
            ),
            ToolSpec(
                "search_text",
                "Search text files and return matching workspace-relative paths and line numbers.",
                {
                    "query": {"type": "string", "minLength": 1},
                    "path": {"type": "string", "minLength": 1},
                    "file_glob": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Example: *.py",
                    },
                    "case_sensitive": {"type": "boolean"},
                    "regex": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                self.search_text,
            ),
            ToolSpec(
                "run_command",
                "Run a non-interactive shell command in the workspace. Credentials are removed from its environment.",
                {
                    "command": {"type": "string", "minLength": 1},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                self.run_command,
            ),
        )

    def execute(self, name: str, arguments: Any) -> str:
        spec = self._specs.get(name) if isinstance(name, str) else None
        if spec is None:
            return self._result(
                False,
                error_code="unknown_tool",
                error=f"未知工具: {name}",
            )
        issues = spec.validate_arguments(arguments)
        if issues:
            return self._result(
                False,
                error_code="invalid_tool_arguments",
                error="工具参数无效: " + "; ".join(issue["message"] for issue in issues),
                details=issues,
            )
        try:
            output = spec.handler(**arguments)
            return self._result(True, output=self._truncate(output))
        except TypeError as exc:
            return self._result(
                False,
                error_code="tool_internal_error",
                error=f"工具内部调用失败: {exc}",
            )
        except (ToolError, OSError, ValueError, re.error) as exc:
            return self._result(
                False,
                error_code="tool_execution_error",
                error=str(exc),
            )

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
