"""Small OpenAI-compatible Chat Completions HTTP client."""

from __future__ import annotations

import json
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from coding_agent.config import Settings
from coding_agent.errors import APIError


class ChatCompletionsClient:
    """Call a Chat Completions endpoint without an API client dependency."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        if settings.base_url.endswith("/chat/completions"):
            self._endpoint = settings.base_url
        else:
            self._endpoint = f"{settings.base_url}/chat/completions"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "model": self._settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "coding-agent/0.1",
            },
        )

        attempts = self._settings.api_retries + 1
        for attempt in range(attempts):
            try:
                with urlopen(request, timeout=self._settings.api_timeout_seconds) as response:
                    response_body = response.read()
                return self._parse_response(response_body)
            except HTTPError as exc:
                error_body = exc.read(8_000).decode("utf-8", errors="replace")
                if exc.code in {408, 409, 429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    self._backoff(attempt)
                    continue
                raise APIError(f"模型 API 返回 HTTP {exc.code}: {error_body}") from exc
            except (URLError, socket.timeout, TimeoutError) as exc:
                if attempt + 1 < attempts:
                    self._backoff(attempt)
                    continue
                raise APIError(f"无法连接模型 API: {exc}") from exc

        raise APIError("模型 API 请求在重试后仍未成功。")

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(0.5 * (2**attempt), 2.0))

    @staticmethod
    def _parse_response(response_body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(response_body.decode("utf-8"))
            choices = data["choices"]
            message = choices[0]["message"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            preview = response_body[:1_000].decode("utf-8", errors="replace")
            raise APIError(f"模型 API 响应格式无效: {preview}") from exc
        if not isinstance(message, dict):
            raise APIError("模型 API 的 message 字段不是对象。")
        return message
