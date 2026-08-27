from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from coding_agent.client import ChatCompletionsClient
from coding_agent.config import Settings
from coding_agent.errors import APIError, ConfigurationError


class FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


class ConfigurationTests(unittest.TestCase):
    def test_reads_openai_environment_variables(self) -> None:
        environment = {
            "OPENAI_API_KEY": "secret",
            "OPENAI_MODEL": "example-model",
            "OPENAI_BASE_URL": "https://gateway.example/v1/",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_sources()
        self.assertEqual(settings.api_key, "secret")
        self.assertEqual(settings.model, "example-model")
        self.assertEqual(settings.base_url, "https://gateway.example/v1")

    def test_missing_model_is_rejected(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_sources()

    def test_invalid_url_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_sources(api_key="key", model="model", base_url="not-a-url")


class ClientTests(unittest.TestCase):
    @patch("coding_agent.client.urlopen")
    def test_sends_chat_completion_payload_and_parses_message(self, mocked_open) -> None:
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        ).encode()
        mocked_open.return_value = FakeHTTPResponse(body)
        settings = Settings(api_key="secret", model="model", base_url="https://api.example/v1")
        client = ChatCompletionsClient(settings)

        message = client.complete([{"role": "user", "content": "hi"}], [])

        self.assertEqual(message["content"], "ok")
        request = mocked_open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "model")
        self.assertEqual(request.full_url, "https://api.example/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")

    def test_invalid_response_is_rejected(self) -> None:
        with self.assertRaises(APIError):
            ChatCompletionsClient._parse_response(b"not json")


if __name__ == "__main__":
    unittest.main()
