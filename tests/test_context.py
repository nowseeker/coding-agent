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


if __name__ == "__main__":
    unittest.main()
