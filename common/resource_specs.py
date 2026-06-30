from typing_extensions import TypedDict


class ResourceSpec(TypedDict, total=False):
    """Typed shape for MCP resource allowlist entries passed on command payloads."""

    uri: str
    description: str
