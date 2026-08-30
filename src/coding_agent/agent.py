"""The model/tool execution loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from coding_agent.context import Conversation
from coding_agent.errors import AgentError, APIError
from coding_agent.tools import WorkspaceTools


class CompletionClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


EventHandler = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class RunResult:
    final_text: str
    iterations: int
    stop_reason: str


class CodingAgent:
    """Drive a model until it returns a final answer or a guard stops it."""

    def __init__(
        self,
        client: CompletionClient,
        tools: WorkspaceTools,
        *,
        max_iterations: int = 24,
        max_context_chars: int = 120_000,
        repeated_call_limit: int = 3,
        event_handler: EventHandler | None = None,
    ) -> None:
        self._client = client
        self._tools = tools
        self._max_iterations = max_iterations
        self._max_context_chars = max_context_chars
        self._repeated_call_limit = repeated_call_limit
        self._event_handler = event_handler or (lambda _kind, _payload: None)

    def run(
        self,
        task: str,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> RunResult:
        if not isinstance(task, str) or not task.strip():
            raise AgentError("任务描述不能为空。")
        self._tools.start_task()
        conversation = Conversation(
            self._system_prompt(),
            task.strip(),
            self._max_context_chars,
            history=history,
        )
        previous_signature: str | None = None
        repeated_count = 0
        rejected_completion_count = 0

        for iteration in range(1, self._max_iterations + 1):
            self._event_handler("iteration", {"number": iteration})
            message = self._client.complete(
                conversation.messages_for_request(),
                self._tools.schemas(),
            )
            assistant = self._normalize_assistant_message(message)
            content = self._text_content(assistant.get("content"))
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                if not content:
                    raise APIError("模型既没有返回文本，也没有返回工具调用。")
                rejected_completion_count += 1
                if rejected_completion_count >= 3:
                    raise AgentError(
                        "模型连续 3 次绕过 finish_task 返回普通文本，已停止任务。"
                    )
                feedback = (
                    "完成门拒绝这段普通回答。你必须使用 finish_task 单独提交结果；"
                    "如果修改了文件，先运行验证命令，并把完全相同的命令填入 "
                    "verification_command。"
                )
                self._event_handler(
                    "completion_rejected",
                    {"reason": feedback, "text": content},
                )
                conversation.add_completion_rejection(assistant, feedback)
                continue
            if content:
                self._event_handler("assistant", {"text": content})
            rejected_completion_count = 0

            parsed_calls = [self._parse_tool_call(call) for call in tool_calls]
            finish_calls = [call for call in parsed_calls if call[1] == "finish_task"]
            if finish_calls and len(parsed_calls) != 1:
                result = json.dumps(
                    {
                        "ok": False,
                        "error_code": "invalid_finish_batch",
                        "error": "finish_task 必须是本轮唯一的工具调用，整批调用均未执行。",
                    },
                    ensure_ascii=False,
                )
                tool_messages = [
                    {"role": "tool", "tool_call_id": call_id, "content": result}
                    for call_id, _name, _arguments in parsed_calls
                ]
                conversation.add_tool_exchange(assistant, tool_messages)
                continue

            signature = self._tool_call_signature(tool_calls)
            if signature == previous_signature:
                repeated_count += 1
            else:
                previous_signature = signature
                repeated_count = 1
            if repeated_count >= self._repeated_call_limit:
                raise AgentError(
                    f"连续 {repeated_count} 轮收到完全相同的工具调用，已停止以避免死循环。"
                )

            tool_messages: list[dict[str, Any]] = []
            for call_id, name, arguments in parsed_calls:
                self._event_handler("tool_start", {"name": name})
                if isinstance(arguments, str):
                    try:
                        decoded_arguments = json.loads(arguments)
                    except json.JSONDecodeError as exc:
                        result = json.dumps(
                            {"ok": False, "error": f"工具 arguments 不是有效 JSON: {exc}"},
                            ensure_ascii=False,
                        )
                    else:
                        result = self._tools.execute(name, decoded_arguments)
                else:
                    result = self._tools.execute(name, arguments)
                self._event_handler(
                    "tool_end",
                    {"name": name, "ok": self._tool_result_ok(result), "result": result},
                )
                tool_messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": result}
                )
            evidence = self._tools.completion_evidence
            if evidence is not None:
                return RunResult(evidence.final_text(), iteration, "verified_completed")
            conversation.add_tool_exchange(assistant, tool_messages)

        raise AgentError(f"达到最大循环次数 {self._max_iterations}，任务仍未结束。")

    def _system_prompt(self) -> str:
        return f"""你是一个在本地工作区中完成编程任务的智能体。
工作区根目录：{self._tools.root}

工作规则：
1. 先检查现有文件和约束，再制定并执行必要修改；不要猜测文件内容。
2. 使用提供的本地工具读取、搜索、写入和测试代码。路径尽量使用工作区相对路径。
3. 写文件前读取相关上下文；修改后运行与风险相称的测试或检查。
4. 工具失败时分析错误并调整参数，不要虚构成功结果。
5. 不读取凭据，不尝试访问工作区之外的文件，不把秘密写入代码或日志。
6. 避免破坏性命令。不得篡改 Git 元数据或声称进行了未实际执行的操作。
7. 不得用普通文本直接宣布完成。完成时必须单独调用 finish_task。
8. 如果修改了文件，必须在最后一次修改之后运行相关测试或检查，并把完全相同的成功命令作为
   verification_command 提交给 finish_task；不得虚构验证记录。
9. finish_task 的 summary 说明改动，limitations 如实说明仍存在的限制。

除 finish_task 必须单独调用外，你可以在一轮中调用多个工具。所有工具都在本机执行，不是
远程托管的代码执行服务。"""

    @staticmethod
    def _normalize_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise APIError("模型 message 不是对象。")
        tool_calls = message.get("tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise APIError("模型 tool_calls 不是数组。")
        normalized: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content"),
        }
        if tool_calls:
            normalized["tool_calls"] = tool_calls
        return normalized

    @staticmethod
    def _text_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text", "")))
            return "\n".join(text_parts).strip()
        return str(content).strip()

    @staticmethod
    def _parse_tool_call(call: Any) -> tuple[str, str, Any]:
        if not isinstance(call, dict):
            raise APIError("模型返回了无效的工具调用对象。")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id:
            raise APIError("工具调用缺少 id。")
        if call.get("type", "function") != "function" or not isinstance(function, dict):
            raise APIError("仅支持 function 类型的工具调用。")
        name = function.get("name")
        arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not name:
            raise APIError("工具调用缺少 function.name。")
        return call_id, name, arguments

    @staticmethod
    def _tool_call_signature(tool_calls: list[Any]) -> str:
        normalized = []
        for call in tool_calls:
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                function = call["function"]
                normalized.append(
                    {
                        "type": call.get("type", "function"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments", "{}"),
                    }
                )
            else:
                normalized.append(call)
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _tool_result_ok(result: str) -> bool:
        try:
            return bool(json.loads(result).get("ok"))
        except (json.JSONDecodeError, AttributeError):
            return False
