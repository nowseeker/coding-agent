"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from coding_agent.errors import ConfigurationError


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_TIMEOUT_SECONDS = 90
DEFAULT_MAX_ITERATIONS = 24
DEFAULT_MAX_CONTEXT_CHARS = 120_000
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 20_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. Credentials intentionally exist only in memory."""

    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    api_timeout_seconds: int = DEFAULT_API_TIMEOUT_SECONDS
    api_retries: int = 2
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    max_tool_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    repeated_call_limit: int = 3

    @classmethod
    def from_sources(
        cls,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_timeout_seconds: int | None = None,
        max_iterations: int | None = None,
        max_context_chars: int | None = None,
        max_tool_output_chars: int | None = None,
        command_timeout_seconds: int | None = None,
    ) -> "Settings":
        """Load CLI overrides first, then agent-specific and OpenAI variables."""

        resolved_key = api_key or os.getenv("CODING_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")
        resolved_model = model or os.getenv("CODING_AGENT_MODEL") or os.getenv("OPENAI_MODEL")
        resolved_url = (
            base_url
            or os.getenv("CODING_AGENT_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL
        )

        if not resolved_key or not resolved_key.strip():
            raise ConfigurationError(
                "缺少 API key；请设置 OPENAI_API_KEY 或 CODING_AGENT_API_KEY。"
            )
        if not resolved_model or not resolved_model.strip():
            raise ConfigurationError(
                "缺少模型名；请设置 OPENAI_MODEL、CODING_AGENT_MODEL 或使用 --model。"
            )

        resolved_url = resolved_url.strip().rstrip("/")
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("base URL 必须是有效的 http(s) 地址。")

        values = {
            "api_timeout_seconds": (
                DEFAULT_API_TIMEOUT_SECONDS
                if api_timeout_seconds is None
                else api_timeout_seconds
            ),
            "max_iterations": (
                DEFAULT_MAX_ITERATIONS if max_iterations is None else max_iterations
            ),
            "max_context_chars": (
                DEFAULT_MAX_CONTEXT_CHARS
                if max_context_chars is None
                else max_context_chars
            ),
            "max_tool_output_chars": (
                DEFAULT_MAX_TOOL_OUTPUT_CHARS
                if max_tool_output_chars is None
                else max_tool_output_chars
            ),
            "command_timeout_seconds": (
                DEFAULT_COMMAND_TIMEOUT_SECONDS
                if command_timeout_seconds is None
                else command_timeout_seconds
            ),
        }
        minimums = {
            "api_timeout_seconds": 1,
            "max_iterations": 1,
            "max_context_chars": 8_000,
            "max_tool_output_chars": 1_000,
            "command_timeout_seconds": 1,
        }
        for name, value in values.items():
            if value < minimums[name]:
                raise ConfigurationError(f"{name} 不能小于 {minimums[name]}。")

        return cls(
            api_key=resolved_key.strip(),
            model=resolved_model.strip(),
            base_url=resolved_url,
            **values,
        )
