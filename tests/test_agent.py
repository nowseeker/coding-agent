from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from coding_agent.agent import CodingAgent
from coding_agent.errors import AgentError, APIError
from coding_agent.tools import WorkspaceTools


class FakeClient:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.responses = list(messages)
        self.requests: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools):
        self.requests.append(messages)
        return self.responses.pop(0)


def tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


class CodingAgentTests(unittest.TestCase):
    def test_agent_executes_tool_then_returns_final_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    tool_call("call_1", "write_file", '{"path":"hello.txt","content":"hi"}'),
                    {"role": "assistant", "content": "任务完成。"},
                ]
            )
            agent = CodingAgent(client, WorkspaceTools(directory))

            result = agent.run("创建 hello.txt")

            self.assertEqual(result.stop_reason, "completed")
            self.assertEqual(result.iterations, 2)
            self.assertEqual(Path(directory, "hello.txt").read_text(encoding="utf-8"), "hi")
            tool_messages = [m for m in client.requests[1] if m["role"] == "tool"]
            self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")
            self.assertTrue(json.loads(tool_messages[0]["content"])["ok"])

    def test_invalid_tool_json_is_returned_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    tool_call("bad", "read_file", "{"),
                    {"role": "assistant", "content": "已处理参数错误。"},
                ]
            )
            agent = CodingAgent(client, WorkspaceTools(directory))

            agent.run("读取文件")

            tool_result = next(m for m in client.requests[1] if m["role"] == "tool")
            self.assertFalse(json.loads(tool_result["content"])["ok"])

    def test_repeated_calls_are_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    tool_call("first-id", "list_files", "{}"),
                    tool_call("second-id", "list_files", "{}"),
                    tool_call("third-id", "list_files", "{}"),
                ]
            )
            agent = CodingAgent(
                client,
                WorkspaceTools(directory),
                repeated_call_limit=3,
            )

            with self.assertRaisesRegex(AgentError, "死循环"):
                agent.run("查看文件")

    def test_final_text_is_not_emitted_as_intermediate_commentary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events: list[tuple[str, dict[str, Any]]] = []
            agent = CodingAgent(
                FakeClient([{"role": "assistant", "content": "最终回答"}]),
                WorkspaceTools(directory),
                event_handler=lambda kind, payload: events.append((kind, payload)),
            )

            result = agent.run("完成任务")

            self.assertEqual(result.final_text, "最终回答")
            self.assertNotIn("assistant", [kind for kind, _payload in events])

    def test_empty_model_message_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = CodingAgent(
                FakeClient([{"role": "assistant", "content": None}]),
                WorkspaceTools(directory),
            )
            with self.assertRaises(APIError):
                agent.run("做事")

    def test_iteration_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    tool_call("one", "list_files", "{}"),
                    tool_call("two", "list_files", "{}"),
                ]
            )
            agent = CodingAgent(client, WorkspaceTools(directory), max_iterations=2)
            with self.assertRaisesRegex(AgentError, "最大循环次数"):
                agent.run("持续查看")

    def test_missing_tool_call_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad_call = {
                "role": "assistant",
                "tool_calls": [{"type": "function", "function": {"name": "list_files", "arguments": "{}"}}],
            }
            agent = CodingAgent(FakeClient([bad_call]), WorkspaceTools(directory))
            with self.assertRaisesRegex(APIError, "缺少 id"):
                agent.run("查看文件")

    def test_completed_history_is_sent_before_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient([{"role": "assistant", "content": "继续完成。"}])
            agent = CodingAgent(client, WorkspaceTools(directory))

            agent.run(
                "现在增加测试",
                history=[
                    {"role": "user", "content": "先创建程序"},
                    {"role": "assistant", "content": "程序已创建"},
                ],
            )

            request = client.requests[0]
            self.assertEqual(
                [(message["role"], message["content"]) for message in request[1:]],
                [
                    ("user", "先创建程序"),
                    ("assistant", "程序已创建"),
                    ("user", "现在增加测试"),
                ],
            )

    def test_tool_event_redacts_full_write_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events: list[tuple[str, dict[str, Any]]] = []
            agent = CodingAgent(
                FakeClient(
                    [
                        tool_call(
                            "write",
                            "write_file",
                            '{"path":"large.txt","content":"generated code"}',
                        ),
                        {"role": "assistant", "content": "完成。"},
                    ]
                ),
                WorkspaceTools(directory),
                event_handler=lambda kind, payload: events.append((kind, payload)),
            )

            agent.run("写文件")

            start = next(payload for kind, payload in events if kind == "tool_start")
            self.assertEqual(start["arguments"]["path"], "large.txt")
            self.assertEqual(start["arguments"]["content"], "[文件内容，共 14 个字符]")


if __name__ == "__main__":
    unittest.main()
