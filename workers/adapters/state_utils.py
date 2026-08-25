"""
Shared helpers for agent adapters that consume message-like objects.

Supports LangChain message mocks and ``ChatMessage`` transcripts where still needed
by structured output parsing and ReAct tool-failure classification.
"""

import json
import re
from typing import Any

from common.utils import tool_call_args_to_dict

__all__ = [
    "tool_call_args_to_dict",
    "tool_output_indicates_failure",
    "tool_output_is_recoverable",
    "parse_json_object_from_assistant_text",
]

_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


def tool_output_indicates_failure(output: str) -> bool:
    """Delegate failure heuristics to the tool plugin registry (enterprise may override)."""
    from common.plugins.registry import get_registry

    return get_registry().tools.tool_output_indicates_failure(output)


def tool_output_is_recoverable(output: str) -> bool:
    """Delegate recoverable-failure heuristics to the tool plugin registry.

    Falls back to the OSS default when a custom plugin omits the hook.
    """
    from common.plugins.registry import get_registry
    from common.tool_failure import default_tool_output_is_recoverable

    tools = get_registry().tools
    hook = getattr(tools, "tool_output_is_recoverable", None)
    if callable(hook):
        return bool(hook(output))
    return default_tool_output_is_recoverable(output)


def parse_json_object_from_assistant_text(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object from assistant text, stripping markdown fences and preamble."""
    stripped = (raw or "").strip()
    if not stripped:
        return None
    fence_match = _FENCE_PATTERN.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
