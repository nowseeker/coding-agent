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


def finish_call(
    call_id: str,
    summary: str,
    verification_command: str = "",
) -> dict[str, Any]:
    return tool_call(
        call_id,
        "finish_task",
        json.dumps(
            {
                "summary": summary,
                "verification_command": verification_command,
            },
            ensure_ascii=False,
        ),
    )


class CodingAgentTests(unittest.TestCase):
    def test_agent_executes_tool_then_returns_final_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verification_command = "python --version"
            client = FakeClient(
                [
                    tool_call("call_1", "write_file", '{"path":"hello.txt","content":"hi"}'),
                    tool_call(
                        "verify",
                        "run_command",
                        json.dumps({"command": verification_command}),
                    ),
                    finish_call("finish", "任务完成。", verification_command),
                ]
            )
            agent = CodingAgent(client, WorkspaceTools(directory))

            result = agent.run("创建 hello.txt")

            self.assertEqual(result.stop_reason, "verified_completed")
            self.assertEqual(result.iterations, 3)
            self.assertIn("任务完成。", result.final_text)
            self.assertIn(f"验证命令：{verification_command}", result.final_text)
            self.assertEqual(Path(directory, "hello.txt").read_text(encoding="utf-8"), "hi")
            tool_messages = [m for m in client.requests[1] if m["role"] == "tool"]
            self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")
            self.assertTrue(json.loads(tool_messages[0]["content"])["ok"])

    def test_invalid_tool_json_is_returned_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    tool_call("bad", "read_file", "{"),
                    tool_call("inspect", "list_files", "{}"),
                    finish_call("finish", "已处理参数错误。"),
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
                FakeClient(
                    [
                        tool_call("inspect", "list_files", "{}"),
                        finish_call("finish", "最终回答"),
                    ]
                ),
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
            client = FakeClient(
                [
                    tool_call("inspect", "list_files", "{}"),
                    finish_call("finish", "继续完成。"),
                ]
            )
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

    def test_plain_final_text_is_rejected_until_finish_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    {"role": "assistant", "content": "我认为已经完成。"},
                    tool_call("inspect", "list_files", "{}"),
                    finish_call("finish", "检查完成。"),
                ]
            )
            events: list[tuple[str, dict[str, Any]]] = []
            agent = CodingAgent(
                client,
                WorkspaceTools(directory),
                event_handler=lambda kind, payload: events.append((kind, payload)),
            )

            result = agent.run("检查项目")

            self.assertEqual(result.stop_reason, "verified_completed")
            self.assertEqual(result.iterations, 3)
            self.assertIn("completion_rejected", [kind for kind, _payload in events])
            second_request = client.requests[1]
            self.assertEqual(second_request[-2]["role"], "assistant")
            self.assertEqual(second_request[-1]["role"], "user")
            self.assertIn("必须使用 finish_task", second_request[-1]["content"])

    def test_finish_task_rejection_is_returned_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verification_command = "python --version"
            client = FakeClient(
                [
                    tool_call("write", "write_file", '{"path":"a.txt","content":"a"}'),
                    finish_call("too_early", "声称完成"),
                    tool_call(
                        "verify",
                        "run_command",
                        json.dumps({"command": verification_command}),
                    ),
                    finish_call("finish", "真实完成", verification_command),
                ]
            )
            agent = CodingAgent(client, WorkspaceTools(directory))

            result = agent.run("写文件")

            self.assertEqual(result.iterations, 4)
            rejected_result = next(
                message
                for message in client.requests[2]
                if message.get("role") == "tool"
                and message.get("tool_call_id") == "too_early"
            )
            self.assertFalse(json.loads(rejected_result["content"])["ok"])
            self.assertIn("必须先运行验证命令", rejected_result["content"])

    def test_finish_task_must_be_the_only_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid_batch = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("inspect", "list_files", "{}")["tool_calls"][0],
                    finish_call("finish_in_batch", "不能完成")["tool_calls"][0],
                ],
            }
            client = FakeClient(
                [
                    invalid_batch,
                    tool_call("inspect_again", "list_files", "{}"),
                    finish_call("finish", "完成检查"),
                ]
            )
            agent = CodingAgent(client, WorkspaceTools(directory))

            result = agent.run("检查项目")

            self.assertEqual(result.iterations, 3)
            batch_results = [
                json.loads(message["content"])
                for message in client.requests[1]
                if message.get("role") == "tool"
            ]
            self.assertEqual(len(batch_results), 2)
            self.assertTrue(
                all(result["error_code"] == "invalid_finish_batch" for result in batch_results)
            )

    def test_repeated_plain_completion_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    {"role": "assistant", "content": "完成 1"},
                    {"role": "assistant", "content": "完成 2"},
                    {"role": "assistant", "content": "完成 3"},
                ]
            )
            agent = CodingAgent(client, WorkspaceTools(directory))

            with self.assertRaisesRegex(AgentError, "绕过 finish_task"):
                agent.run("执行任务")


if __name__ == "__main__":
    unittest.main()
