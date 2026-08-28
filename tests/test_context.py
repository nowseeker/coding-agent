from __future__ import annotations

import unittest

from coding_agent.context import Conversation


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


if __name__ == "__main__":
    unittest.main()
