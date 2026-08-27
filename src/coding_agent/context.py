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
    """Keep complete tool-call blocks so requests always remain protocol-valid."""

    def __init__(self, system_prompt: str, user_prompt: str, max_chars: int) -> None:
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt
        self._max_chars = max_chars
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

        selected_reversed: list[list[dict[str, Any]]] = []
        selected_size = 0
        for block in reversed(self._blocks):
            block_size = sum(_message_size(message) for message in block)
            if selected_reversed and selected_size + block_size > available:
                break
            if not selected_reversed and block_size > available:
                selected_reversed.append(self._shrink_block(block, available))
                selected_size = available
                break
            selected_reversed.append(deepcopy(block))
            selected_size += block_size

        selected = list(reversed(selected_reversed))
        omitted_count = len(self._blocks) - len(selected)
        if omitted_count:
            summary = self._summarize_omitted(self._blocks[:omitted_count])
            summary_budget = max(self._max_chars - base_size - selected_size, 0)
            if summary_budget >= 200:
                base_system["content"] += "\n\n" + _clip(summary, summary_budget)

        messages = [base_system, user]
        for block in selected:
            messages.extend(block)
        return messages

    @staticmethod
    def _shrink_block(block: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
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
