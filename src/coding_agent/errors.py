"""Domain-specific exceptions with user-facing messages."""


class AgentError(Exception):
    """Base exception for a controlled agent failure."""


class ConfigurationError(AgentError):
    """Raised when required configuration is missing or invalid."""


class APIError(AgentError):
    """Raised when the model endpoint cannot return a usable response."""


class ToolError(AgentError):
    """Raised when a local tool request is unsafe or cannot be completed."""
