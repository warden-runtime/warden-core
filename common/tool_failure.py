"""Default tool-output failure heuristics (OSS; override via ToolLifecycleHooks)."""

from __future__ import annotations


def default_tool_output_indicates_failure(output: str) -> bool:
    """Return True when tool return text matches built-in failure patterns."""
    if not output or not isinstance(output, str):
        return False
    lowered = output.strip().lower()
    return (
        lowered.startswith("mcp error")
        or lowered.startswith("error:")
        or "input validation error" in lowered
        or "invalid arguments for tool" in lowered
    )


def plain_text_tool_result_looks_like_error(raw: str) -> bool:
    """True when non-JSON tool text likely indicates a transport/API failure (facts path only)."""
    if not raw or not isinstance(raw, str):
        return False
    stripped = raw.strip()
    if not stripped or stripped[0] in "{[":
        return False
    lowered = stripped.lower()
    return (
        lowered.startswith("failed to ")
        or lowered.startswith("could not ")
        or default_tool_output_indicates_failure(stripped)
    )
