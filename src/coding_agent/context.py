"""Conversation storage with deterministic execution-trace compaction."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


def _message_size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n...[截断 {omitted} 个字符]"


class Conversation:
    """Keep recent dialogue and complete tool blocks within a character budget."""

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

    def messages_for_request(self) -> list[dict[str, Any]]:
        base_system = {"role": "system", "content": self._system_prompt}
        user = {"role": "user", "content": self._user_prompt}
        base_size = _message_size(base_system) + _message_size(user)
        available = max(self._max_chars - base_size, 0)

        selected_tools, tool_size = self._select_recent_blocks(
            self._blocks,
            available,
            shrink=self._shrink_tool_block,
        )
        history_budget = max(available - tool_size, 0)
        selected_history, history_size = self._select_recent_blocks(
            self._history_blocks,
            history_budget,
            shrink=self._shrink_history_block,
        )

        omitted_history = self._history_blocks[: len(self._history_blocks) - len(selected_history)]
        omitted_tools = self._blocks[: len(self._blocks) - len(selected_tools)]
        summary_parts = []
        if omitted_history:
            summary_parts.append(self._summarize_history(omitted_history))
        if omitted_tools:
            summary_parts.append(self._summarize_omitted(omitted_tools))
        summary_budget = max(
            self._max_chars - base_size - tool_size - history_size,
            0,
        )
        if summary_parts and summary_budget >= 200:
            base_system["content"] += "\n\n" + _clip("\n\n".join(summary_parts), summary_budget)

        messages = [base_system]
        for block in selected_history:
            messages.extend(block)
        messages.append(user)
        for block in selected_tools:
            messages.extend(block)
        return messages

    @staticmethod
    def _select_recent_blocks(
        blocks: list[list[dict[str, Any]]],
        budget: int,
        *,
        shrink,
    ) -> tuple[list[list[dict[str, Any]]], int]:
        selected_reversed: list[list[dict[str, Any]]] = []
        selected_size = 0
        for block in reversed(blocks):
            block_size = sum(_message_size(message) for message in block)
            if selected_reversed and selected_size + block_size > budget:
                break
            if not selected_reversed and block_size > budget:
                compact = shrink(block, budget)
                if compact:
                    selected_reversed.append(compact)
                    selected_size = sum(_message_size(message) for message in compact)
                break
            selected_reversed.append(deepcopy(block))
            selected_size += block_size
        return list(reversed(selected_reversed)), selected_size

    @staticmethod
    def _shrink_tool_block(block: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        if not block:
            return []
        result = [deepcopy(block[0])]
        remaining = max(budget - _message_size(result[0]), 0)
        tool_messages = block[1:]
        per_tool = max(remaining // max(len(tool_messages), 1) - 100, 80)
        for message in tool_messages:
            compact = deepcopy(message)
            content = compact.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            compact["content"] = _clip(content, per_tool)
            result.append(compact)
        return result

    @staticmethod
    def _shrink_history_block(
        block: list[dict[str, Any]], budget: int
    ) -> list[dict[str, Any]]:
        if not block or budget < 200:
            return []
        per_message = max(budget // len(block) - 100, 80)
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
                lines.append(f"- {label}: {_clip(str(message.get('content', '')), 240)}")
        return "\n".join(lines)

    @staticmethod
    def _summarize_omitted(blocks: list[list[dict[str, Any]]]) -> str:
        lines = ["较早的执行轨迹已压缩；以下内容只来自实际工具调用："]
        for block in blocks:
            if not block:
                continue
            assistant = block[0]
            calls = assistant.get("tool_calls") or []
            results = {
                message.get("tool_call_id"): message.get("content", "")
                for message in block[1:]
            }
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name", "unknown")
                arguments = function.get("arguments", "{}")
                result = results.get(call.get("id"), "")
                lines.append(
                    f"- {name}({_clip(str(arguments), 180)}): {_clip(str(result), 240)}"
                )
        return "\n".join(lines)
