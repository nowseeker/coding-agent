"""Conversation storage with protocol-safe, tool-aware context compaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from typing import Any

from coding_agent.errors import ContextBudgetError


def serialized_size(value: Any) -> int:
    """Return the exact compact-JSON character count used for budget checks."""

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = f"...[截断 {len(text) - limit} 个字符]"
    if len(marker) >= limit:
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n...[中间截断 {len(text) - limit} 个字符]...\n"
    if len(marker) >= limit:
        return _clip(text, limit)
    remaining = limit - len(marker)
    head = (remaining * 2) // 3
    return text[:head] + marker + text[-(remaining - head) :]


class Conversation:
    """Keep a raw local ledger and build a compact model-facing projection."""

    def __init__(
        self,
        system_prompt: str,
        user_prompt: str,
        max_chars: int,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt
        self._max_chars = max_chars
        self._history_blocks = self._normalize_history(history or [])
        self._blocks: list[list[dict[str, Any]]] = []

    @property
    def blocks(self) -> tuple[tuple[dict[str, Any], ...], ...]:
        """Expose copies of the uncompressed execution ledger for inspection."""

        return tuple(tuple(deepcopy(message) for message in block) for block in self._blocks)

    def add_tool_exchange(
        self,
        assistant_message: dict[str, Any],
        tool_messages: list[dict[str, Any]],
    ) -> None:
        if assistant_message.get("role") != "assistant":
            raise ValueError("assistant_message must have the assistant role")
        if not assistant_message.get("tool_calls"):
            raise ValueError("a tool exchange must contain tool_calls")
        self._blocks.append([deepcopy(assistant_message), *deepcopy(tool_messages)])

    def add_completion_rejection(
        self,
        assistant_message: dict[str, Any],
        feedback: str,
    ) -> None:
        """Keep an unverified answer and the local gate's corrective feedback."""

        if assistant_message.get("role") != "assistant":
            raise ValueError("assistant_message must have the assistant role")
        self._blocks.append(
            [deepcopy(assistant_message), {"role": "user", "content": feedback}]
        )

    def messages_for_request(self, *, reserved_chars: int = 0) -> list[dict[str, Any]]:
        """Build one request without exceeding the post-reservation character budget."""

        if reserved_chars < 0:
            raise ValueError("reserved_chars cannot be negative")
        message_budget = self._max_chars - reserved_chars
        base_system = {"role": "system", "content": self._system_prompt}
        user = {"role": "user", "content": self._user_prompt}
        if message_budget <= 0 or serialized_size([base_system, user]) > message_budget:
            raise ContextBudgetError(
                "上下文预算不足：系统规则、当前任务、工具 Schema 和回复预留空间无法同时放入请求。"
            )

        views = self._tool_block_views()
        has_optional_context = bool(views or self._history_blocks)
        summary_reserve = (
            min(3_000, max(400, message_budget // 12)) if has_optional_context else 0
        )
        selection_budget = message_budget - summary_reserve

        selected_tools = self._select_tool_suffix(
            views, base_system, user, selection_budget, message_budget
        )
        selected_history = self._select_history_suffix(
            self._history_blocks,
            selected_tools,
            base_system,
            user,
            selection_budget,
        )

        omitted_history = self._history_blocks[
            : len(self._history_blocks) - len(selected_history)
        ]
        omitted_tools = self._blocks[: len(self._blocks) - len(selected_tools)]
        summary_parts: list[str] = []
        if omitted_history:
            summary_parts.append(self._summarize_history(omitted_history))
        if omitted_tools:
            summary_parts.append(self._summarize_omitted(omitted_tools))

        messages = self._compose(base_system, selected_history, user, selected_tools)
        if summary_parts:
            messages = self._add_summary_within_budget(
                messages, "\n\n".join(summary_parts), message_budget
            )
        if serialized_size(messages) > message_budget:
            raise ContextBudgetError("上下文压缩后仍超过请求预算，已停止而不是发送超限请求。")
        return messages

    @staticmethod
    def _compose(
        system: dict[str, Any],
        history: list[list[dict[str, Any]]],
        user: dict[str, Any],
        tools: list[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        messages = [deepcopy(system)]
        for block in history:
            messages.extend(deepcopy(block))
        messages.append(deepcopy(user))
        for block in tools:
            messages.extend(deepcopy(block))
        return messages

    def _select_tool_suffix(
        self,
        views: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]],
        system: dict[str, Any],
        user: dict[str, Any],
        budget: int,
        hard_budget: int,
    ) -> list[list[dict[str, Any]]]:
        selected: list[list[dict[str, Any]]] = []
        for normal, aggressive in reversed(views):
            candidate = [normal, *selected]
            if serialized_size(self._compose(system, [], user, candidate)) <= budget:
                selected = candidate
                continue
            if not selected:
                candidate = [aggressive]
                if serialized_size(
                    self._compose(system, [], user, candidate)
                ) <= hard_budget:
                    selected = candidate
                else:
                    raise ContextBudgetError(
                        "最新工具调用块即使压缩后也无法放入上下文；请提高 --context-chars。"
                    )
            break
        return selected

    def _select_history_suffix(
        self,
        blocks: list[list[dict[str, Any]]],
        tools: list[list[dict[str, Any]]],
        system: dict[str, Any],
        user: dict[str, Any],
        budget: int,
    ) -> list[list[dict[str, Any]]]:
        selected: list[list[dict[str, Any]]] = []
        for block in reversed(blocks):
            candidate = [deepcopy(block), *selected]
            if serialized_size(self._compose(system, candidate, user, tools)) <= budget:
                selected = candidate
                continue
            if not selected:
                compact = self._shrink_history_block(block, 1_200)
                candidate = [compact] if compact else []
                if candidate and serialized_size(
                    self._compose(system, candidate, user, tools)
                ) <= budget:
                    selected = candidate
            break
        return selected

    def _tool_block_views(
        self,
    ) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        latest_mutation: dict[str, int] = {}
        for index, block in enumerate(self._blocks):
            for name, arguments, ok in self._calls_with_status(block):
                if ok and name in {"write_file", "replace_in_file"}:
                    path = arguments.get("path") if isinstance(arguments, dict) else None
                    if isinstance(path, str):
                        latest_mutation[_path_key(path)] = index

        views = []
        for index, block in enumerate(self._blocks):
            stale_paths = {
                path
                for path, mutation_index in latest_mutation.items()
                if mutation_index > index
            }
            views.append(
                (
                    self._compact_tool_block(block, stale_paths, aggressive=False),
                    self._compact_tool_block(block, stale_paths, aggressive=True),
                )
            )
        return views

    @classmethod
    def _compact_tool_block(
        cls,
        block: list[dict[str, Any]],
        stale_paths: set[str],
        *,
        aggressive: bool,
    ) -> list[dict[str, Any]]:
        if not block:
            return []
        assistant = deepcopy(block[0])
        calls = assistant.get("tool_calls") or []
        if not calls:
            assistant["content"] = _clip(
                str(assistant.get("content", "")), 240 if aggressive else 800
            )
            result = [assistant]
            for message in block[1:]:
                compact = deepcopy(message)
                compact["content"] = _clip(
                    str(compact.get("content", "")), 500 if aggressive else 1_200
                )
                result.append(compact)
            return result

        result_by_id = {
            message.get("tool_call_id"): message
            for message in block[1:]
            if message.get("role") == "tool"
        }
        assistant["content"] = (
            _clip(str(assistant.get("content") or ""), 200 if aggressive else 600)
            or None
        )
        compact_calls = []
        call_metadata: dict[str, tuple[str, dict[str, Any] | None, bool]] = {}
        for call in calls:
            compact_call = deepcopy(call)
            function = compact_call.get("function")
            if not isinstance(function, dict):
                compact_calls.append(compact_call)
                continue
            name = str(function.get("name", "unknown"))
            arguments = cls._decode_arguments(function.get("arguments", "{}"))
            tool_message = result_by_id.get(call.get("id"), {})
            payload = cls._decode_result(tool_message.get("content", ""))
            ok = bool(payload.get("ok")) if isinstance(payload, dict) else False
            call_metadata[str(call.get("id", ""))] = (name, arguments, ok)
            if arguments is not None:
                function["arguments"] = json.dumps(
                    cls._compact_arguments(name, arguments, ok, aggressive),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            elif aggressive:
                function["arguments"] = _clip(str(function.get("arguments", "")), 300)
            compact_calls.append(compact_call)
        assistant["tool_calls"] = compact_calls

        compact_block = [assistant]
        for message in block[1:]:
            compact = deepcopy(message)
            call_id = str(compact.get("tool_call_id", ""))
            name, arguments, _ok = call_metadata.get(call_id, ("unknown", None, False))
            path = arguments.get("path") if isinstance(arguments, dict) else None
            stale = (
                name == "read_file"
                and isinstance(path, str)
                and _path_key(path) in stale_paths
            )
            compact["content"] = cls._compact_result(
                name,
                compact.get("content", ""),
                stale=stale,
                aggressive=aggressive,
                path=path if isinstance(path, str) else "",
            )
            compact_block.append(compact)
        return compact_block

    @staticmethod
    def _decode_arguments(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return deepcopy(value)
        if not isinstance(value, str):
            return None
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    @staticmethod
    def _decode_result(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return deepcopy(value)
        if not isinstance(value, str):
            return None
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    @staticmethod
    def _compact_arguments(
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        aggressive: bool,
    ) -> dict[str, Any]:
        compact = deepcopy(arguments)
        if name == "write_file" and isinstance(compact.get("content"), str):
            content = compact["content"]
            if ok or aggressive:
                compact["content"] = (
                    f"[历史写入内容已压缩 chars={len(content)} sha256={_sha256(content)}；"
                    "当前真实代码请重新 read_file]"
                )
        if name == "replace_in_file":
            for field in ("old", "new"):
                value = compact.get(field)
                if isinstance(value, str) and (ok or aggressive):
                    compact[field] = (
                        f"[历史 {field} 已压缩 chars={len(value)} sha256={_sha256(value)}；"
                        "当前真实代码请重新 read_file]"
                    )
        return compact

    @classmethod
    def _compact_result(
        cls,
        name: str,
        content: Any,
        *,
        stale: bool,
        aggressive: bool,
        path: str,
    ) -> str:
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        payload = cls._decode_result(text)
        if payload is None:
            # Malformed/non-JSON results are unusual and may contain the only useful
            # diagnostic. Keep them generously in the normal view; the aggressive
            # view still guarantees that the newest protocol block can fit.
            return _clip(text, 1_200 if aggressive else 12_000)
        if stale and payload.get("ok"):
            payload["output"] = (
                f"[旧读取已失效：{path} 后续已被修改；需要精确代码时请重新按行读取。]"
            )
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        ok = bool(payload.get("ok"))
        if not ok:
            limit = 2_400 if aggressive else 6_000
        elif name == "read_file":
            limit = 1_800 if aggressive else 12_000
        elif name in {"search_text", "inspect_code"}:
            limit = 1_800 if aggressive else 6_000
        elif name == "list_files":
            limit = 1_200 if aggressive else 4_000
        elif name == "run_command":
            output = payload.get("output")
            exit_match = (
                re.match(r"exit_code: (-?\d+)", output)
                if isinstance(output, str)
                else None
            )
            failed_command = exit_match is not None and int(exit_match.group(1)) != 0
            limit = (2_400 if aggressive else 6_000) if failed_command else (
                1_500 if aggressive else 4_000
            )
        else:
            limit = 1_000 if aggressive else 3_000
        if isinstance(payload.get("output"), str):
            payload["output"] = (
                _head_tail(payload["output"], limit)
                if name == "run_command"
                else _clip(payload["output"], limit)
            )
        if isinstance(payload.get("error"), str):
            payload["error"] = _head_tail(payload["error"], limit)
        maximum = limit + 1_000
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(compact) > maximum and "details" in payload:
            details_text = json.dumps(payload["details"], ensure_ascii=False)
            payload["details"] = (
                f"[错误细节已压缩 chars={len(details_text)} sha256={_sha256(details_text)}]"
            )
            compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(compact) > maximum:
            minimal = {"ok": payload.get("ok", False)}
            for field in ("error_code", "error", "output"):
                if field in payload:
                    value = payload[field]
                    minimal[field] = _clip(str(value), max(80, maximum // 3))
            compact = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
        return compact

    @classmethod
    def _calls_with_status(
        cls, block: list[dict[str, Any]]
    ) -> list[tuple[str, dict[str, Any] | None, bool]]:
        if not block:
            return []
        results = {
            str(message.get("tool_call_id", "")): cls._decode_result(message.get("content", ""))
            for message in block[1:]
        }
        calls = []
        for call in block[0].get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            payload = results.get(str(call.get("id", "")))
            calls.append(
                (
                    str(function.get("name", "unknown")),
                    cls._decode_arguments(function.get("arguments", "{}")),
                    bool(payload.get("ok")) if isinstance(payload, dict) else False,
                )
            )
        return calls

    @staticmethod
    def _shrink_history_block(
        block: list[dict[str, Any]], budget: int
    ) -> list[dict[str, Any]]:
        if not block or budget < 200:
            return []
        per_message = max(budget // len(block) - 80, 80)
        result = []
        for message in block:
            compact = deepcopy(message)
            compact["content"] = _clip(str(compact.get("content", "")), per_message)
            result.append(compact)
        return result

    @staticmethod
    def _normalize_history(
        history: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        if not isinstance(history, list):
            raise ValueError("history must be a list")
        blocks: list[list[dict[str, Any]]] = []
        pending_user: dict[str, Any] | None = None
        for message in history:
            if not isinstance(message, dict):
                raise ValueError("history messages must be objects")
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            normalized = {"role": role, "content": content}
            if role == "user":
                pending_user = normalized
            elif pending_user is not None:
                blocks.append([pending_user, normalized])
                pending_user = None
        return blocks

    @staticmethod
    def _summarize_history(blocks: list[list[dict[str, Any]]]) -> str:
        lines = ["较早的用户对话已压缩；以下摘要来自已保存的真实消息："]
        for block in blocks:
            for message in block:
                label = "用户" if message.get("role") == "user" else "助手"
                lines.append(f"- {label}: {_clip(str(message.get('content', '')), 220)}")
        return "\n".join(lines)

    @classmethod
    def _summarize_omitted(cls, blocks: list[list[dict[str, Any]]]) -> str:
        lines = ["较早的执行轨迹已压缩；以下内容只来自真实工具调用："]
        for block in blocks:
            if not block:
                continue
            assistant = block[0]
            calls = assistant.get("tool_calls") or []
            if not calls:
                feedback = block[1].get("content", "") if len(block) > 1 else ""
                lines.append(f"- 完成请求曾被拒绝：{_clip(str(feedback), 220)}")
                continue
            results = {
                str(message.get("tool_call_id", "")): cls._decode_result(
                    message.get("content", "")
                )
                for message in block[1:]
            }
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name", "unknown"))
                arguments = cls._decode_arguments(function.get("arguments", "{}")) or {}
                safe_arguments = deepcopy(arguments)
                for field in ("content", "old", "new"):
                    value = safe_arguments.get(field)
                    if isinstance(value, str):
                        safe_arguments[field] = (
                            f"[已省略 chars={len(value)} sha256={_sha256(value)}]"
                        )
                payload = results.get(str(call.get("id", "")))
                state = "成功" if isinstance(payload, dict) and payload.get("ok") else "失败"
                arguments_text = json.dumps(
                    safe_arguments, ensure_ascii=False, separators=(",", ":")
                )
                lines.append(f"- {name}({_clip(arguments_text, 220)})：{state}")
        return "\n".join(lines)

    @staticmethod
    def _add_summary_within_budget(
        messages: list[dict[str, Any]], summary: str, budget: int
    ) -> list[dict[str, Any]]:
        result = deepcopy(messages)
        original = str(result[0].get("content", ""))
        low, high = 0, len(summary)
        best = deepcopy(result)
        while low <= high:
            middle = (low + high) // 2
            candidate = deepcopy(result)
            candidate[0]["content"] = original + "\n\n" + _clip(summary, middle)
            if serialized_size(candidate) <= budget:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best
