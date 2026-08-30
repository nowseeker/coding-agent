from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.client import ChatCompletionsClient
from coding_agent.config import Settings
from coding_agent.errors import APIError
from coding_agent.trace import APITraceRecorder


class FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


class APITraceRecorderTests(unittest.TestCase):
    def test_one_stable_trace_file_is_used_per_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace_directory = root / "traces"
            first_workspace = root / "project one"
            second_workspace = root / "project two"
            first_workspace.mkdir()
            second_workspace.mkdir()

            first = APITraceRecorder(trace_directory, first_workspace)
            same_project = APITraceRecorder(trace_directory, first_workspace)
            second = APITraceRecorder(trace_directory, second_workspace)

            self.assertEqual(first.file_path, same_project.file_path)
            self.assertNotEqual(first.file_path, second.file_path)
            self.assertTrue(first.file_path.is_file())
            self.assertTrue(second.file_path.is_file())

    @patch("coding_agent.client.urlopen")
    def test_client_records_complete_request_and_response_without_api_key(
        self,
        mocked_open,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = APITraceRecorder(root / "traces", workspace)
            response_data = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "记录成功",
                        }
                    }
                ]
            }
            mocked_open.return_value = FakeHTTPResponse(
                json.dumps(response_data, ensure_ascii=False).encode("utf-8")
            )
            settings = Settings(
                api_key="must-not-appear-in-trace",
                model="example-model",
                base_url="https://api.example/v1",
            )
            client = ChatCompletionsClient(settings, recorder)
            messages = [{"role": "user", "content": "检查每轮输入"}]
            tools = [{"type": "function", "function": {"name": "example"}}]

            message = client.complete(messages, tools)

            self.assertEqual(message["content"], "记录成功")
            trace_text = recorder.file_path.read_text(encoding="utf-8")
            self.assertNotIn("must-not-appear-in-trace", trace_text)
            events = [json.loads(line) for line in trace_text.splitlines()]
            request = next(event for event in events if event["event"] == "api_request")
            response = next(event for event in events if event["event"] == "api_response")
            self.assertEqual(request["payload"]["messages"], messages)
            self.assertEqual(request["payload"]["tools"], tools)
            self.assertEqual(request["payload"]["tool_choice"], "auto")
            self.assertEqual(response["response"], response_data)
            self.assertEqual(request["request_id"], response["request_id"])

    def test_non_json_response_is_kept_as_text_for_manual_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = APITraceRecorder(root / "traces", workspace)
            request_id = recorder.record_request({"model": "example"})

            recorder.record_response(
                request_id,
                b"not-json-response",
                attempt=1,
            )

            events = [
                json.loads(line)
                for line in recorder.file_path.read_text(encoding="utf-8").splitlines()
            ]
            response = next(event for event in events if event["event"] == "api_response")
            self.assertEqual(response["response"], "not-json-response")

    @patch("coding_agent.client.urlopen")
    def test_client_marks_invalid_response_as_parse_error(self, mocked_open) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            recorder = APITraceRecorder(root / "traces", workspace)
            mocked_open.return_value = FakeHTTPResponse(b"invalid-json")
            client = ChatCompletionsClient(
                Settings(
                    api_key="secret",
                    model="example-model",
                    base_url="https://api.example/v1",
                ),
                recorder,
            )

            with self.assertRaises(APIError):
                client.complete([{"role": "user", "content": "test"}], [])

            events = [
                json.loads(line)
                for line in recorder.file_path.read_text(encoding="utf-8").splitlines()
            ]
            error = next(event for event in events if event["event"] == "api_error")
            self.assertEqual(error["error_type"], "response_parse_error")
            self.assertFalse(error["retryable"])


if __name__ == "__main__":
    unittest.main()
