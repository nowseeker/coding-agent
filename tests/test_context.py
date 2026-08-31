from __future__ import annotations

import unittest
import json

from coding_agent.context import Conversation, serialized_size
from coding_agent.errors import ContextBudgetError


def exchange(number: int, result_size: int = 20):
    call_id = f"call_{number}"
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": f'{{"path":"{number}.txt"}}'},
            }
        ],
    }
    tool = {"role": "tool", "tool_call_id": call_id, "content": "x" * result_size}
    return assistant, [tool]


class ConversationTests(unittest.TestCase):
    def test_keeps_complete_tool_protocol_blocks(self) -> None:
        conversation = Conversation("rules", "task", 8_000)
        for number in range(3):
            conversation.add_tool_exchange(*exchange(number, 4_000))

        messages = conversation.messages_for_request()

        call_ids = {
            call["id"]
            for message in messages
            if message["role"] == "assistant"
            for call in message["tool_calls"]
        }
        result_ids = {
            message["tool_call_id"] for message in messages if message["role"] == "tool"
        }
        self.assertEqual(call_ids, result_ids)
        self.assertIn("执行轨迹已压缩", messages[0]["content"])

    def test_latest_oversized_block_is_clipped_not_dropped(self) -> None:
        conversation = Conversation("rules", "task", 8_000)
        conversation.add_tool_exchange(*exchange(1, 20_000))

        messages = conversation.messages_for_request()

        self.assertEqual(messages[-2]["role"], "assistant")
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertIn("截断", messages[-1]["content"])

    def test_compaction_keeps_a_contiguous_recent_suffix(self) -> None:
        conversation = Conversation("rules", "task", 8_000)
        conversation.add_tool_exchange(*exchange(1, 20))
        conversation.add_tool_exchange(*exchange(2, 7_800))
        conversation.add_tool_exchange(*exchange(3, 20))

        messages = conversation.messages_for_request()

        retained_ids = [
            call["id"]
            for message in messages
            if message["role"] == "assistant"
            for call in message["tool_calls"]
        ]
        self.assertEqual(retained_ids, ["call_3"])
        self.assertIn('"path":"1.txt"', messages[0]["content"])
        self.assertIn('"path":"2.txt"', messages[0]["content"])

    def test_completed_dialogue_history_precedes_current_task(self) -> None:
        conversation = Conversation(
            "rules",
            "current task",
            8_000,
            history=[
                {"role": "user", "content": "previous task"},
                {"role": "assistant", "content": "previous result"},
                {"role": "error", "content": "must be ignored"},
            ],
        )

        messages = conversation.messages_for_request()

        self.assertEqual(
            [(message["role"], message["content"]) for message in messages[1:]],
            [
                ("user", "previous task"),
                ("assistant", "previous result"),
                ("user", "current task"),
            ],
        )

    def test_history_compaction_keeps_recent_complete_pair(self) -> None:
        conversation = Conversation(
            "rules",
            "current",
            8_000,
            history=[
                {"role": "user", "content": "old task " + "x" * 12_000},
                {"role": "assistant", "content": "old result"},
                {"role": "user", "content": "recent task"},
                {"role": "assistant", "content": "recent result"},
            ],
        )

        messages = conversation.messages_for_request()

        roles_and_text = [(message["role"], message["content"]) for message in messages[1:]]
        self.assertEqual(
            roles_and_text,
            [
                ("user", "recent task"),
                ("assistant", "recent result"),
                ("user", "current"),
            ],
        )
        self.assertIn("较早的用户对话已压缩", messages[0]["content"])

    def test_successful_write_content_is_replaced_by_hash_marker(self) -> None:
        content = "secret implementation line\n" * 400
        call_id = "write"
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": "app.py", "content": content},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }
        tool = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"ok": True, "output": "已写入 app.py"}),
        }
        conversation = Conversation("rules", "task", 30_000)
        conversation.add_tool_exchange(assistant, [tool])

        messages = conversation.messages_for_request()
        sent_arguments = json.loads(messages[-2]["tool_calls"][0]["function"]["arguments"])

        self.assertIn("历史写入内容已压缩", sent_arguments["content"])
        self.assertNotIn("secret implementation line", sent_arguments["content"])
        raw_arguments = conversation.blocks[0][0]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("secret implementation line", raw_arguments)

    def test_read_before_later_write_is_marked_stale(self) -> None:
        conversation = Conversation("rules", "task", 30_000)
        read_assistant, _ = exchange(1)
        read_assistant["tool_calls"][0]["function"]["arguments"] = '{"path":"app.py"}'
        read_tool = {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"ok": True, "output": "1: old code"}),
        }
        conversation.add_tool_exchange(read_assistant, [read_tool])
        write_assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "write",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"app.py","content":"new code"}',
                    },
                }
            ],
        }
        write_tool = {
            "role": "tool",
            "tool_call_id": "write",
            "content": json.dumps({"ok": True, "output": "written"}),
        }
        conversation.add_tool_exchange(write_assistant, [write_tool])

        messages = conversation.messages_for_request()
        old_read = next(
            message for message in messages if message.get("tool_call_id") == "call_1"
        )

        self.assertIn("旧读取已失效", old_read["content"])
        self.assertNotIn("old code", old_read["content"])

    def test_reserved_space_is_included_in_strict_budget(self) -> None:
        conversation = Conversation("rules", "task", 8_000)
        conversation.add_tool_exchange(*exchange(1, 20_000))

        messages = conversation.messages_for_request(reserved_chars=2_000)

        self.assertLessEqual(serialized_size(messages), 6_000)

    def test_mandatory_content_fails_instead_of_exceeding_budget(self) -> None:
        conversation = Conversation("rules", "task", 8_000)

        with self.assertRaises(ContextBudgetError):
            conversation.messages_for_request(reserved_chars=7_990)

    def test_compacted_structured_error_remains_valid_json(self) -> None:
        assistant, _ = exchange(1)
        error = {
            "ok": False,
            "error_code": "invalid_tool_arguments",
            "error": "bad argument",
            "details": [{"message": "x" * 10_000}],
        }
        tool = {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps(error),
        }
        conversation = Conversation("rules", "task", 12_000)
        conversation.add_tool_exchange(assistant, [tool])

        messages = conversation.messages_for_request()
        compacted = next(message for message in messages if message["role"] == "tool")
        payload = json.loads(compacted["content"])

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "invalid_tool_arguments")
        self.assertIn("错误细节已压缩", payload["details"])

    def test_failed_command_compaction_keeps_stderr_tail(self) -> None:
        call_id = "command"
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": '{"command":"python tests.py"}',
                    },
                }
            ],
        }
        output = "exit_code: 1\nstdout:\n" + "x" * 10_000 + "\nstderr:\nTAIL_ERROR"
        tool = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"ok": True, "output": output}),
        }
        conversation = Conversation("rules", "task", 20_000)
        conversation.add_tool_exchange(assistant, [tool])

        messages = conversation.messages_for_request()
        compacted = next(message for message in messages if message["role"] == "tool")
        payload = json.loads(compacted["content"])

        self.assertIn("exit_code: 1", payload["output"])
        self.assertIn("TAIL_ERROR", payload["output"])
        self.assertIn("中间截断", payload["output"])


if __name__ == "__main__":
    unittest.main()
