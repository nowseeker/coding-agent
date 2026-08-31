"""Safe runtime configuration diagnostics for the desktop UI."""

from __future__ import annotations

import os
from typing import Any

from coding_agent.config import DEFAULT_BASE_URL


_EXPECTED_ENVIRONMENT_NAMES = {
    "CODING_AGENT_API_KEY",
    "CODING_AGENT_MODEL",
    "CODING_AGENT_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
}


def runtime_status() -> dict[str, Any]:
    """Describe model configuration without returning any credential value."""

    agent_key = os.getenv("CODING_AGENT_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    key_source = (
        "CODING_AGENT_API_KEY"
        if agent_key
        else "OPENAI_API_KEY" if openai_key else None
    )
    agent_model = os.getenv("CODING_AGENT_MODEL")
    openai_model = os.getenv("OPENAI_MODEL")
    model_source = (
        "CODING_AGENT_MODEL"
        if agent_model
        else "OPENAI_MODEL" if openai_model else None
    )
    model = agent_model or openai_model
    base_url = (
        os.getenv("CODING_AGENT_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    misnamed_variables = sorted(
        name
        for name in os.environ
        if "\\_" in name
        and name.replace("\\_", "_") in _EXPECTED_ENVIRONMENT_NAMES
    )
    return {
        "api_ready": bool(key_source and model),
        "key_configured": bool(key_source),
        "key_source": key_source,
        "model": model,
        "model_source": model_source,
        "base_url": base_url,
        "misnamed_variables": misnamed_variables,
    }
