"""
Shared helpers for agent adapters that consume a list of message-like objects (e.g. after an agent run).

Supports LangChain message mocks (``.type``, ``.tool_calls`` dicts) and ``ChatMessage`` transcripts.
"""

import json
import logging
import re
from typing import Any

from common.agent_adapter import ExecutionStepError
from common.llm import ChatMessage
from common.utils import tool_call_args_to_dict

logger = logging.getLogger(__name__)

__all__ = [
    "tool_call_args_to_dict",
    "tool_output_indicates_failure",
    "extract_submit_payload",
    "check_allowlist_in_state",
    "check_tool_failures_in_state",
    "fallback_output_from_last_assistant_content",
    "parse_json_object_from_assistant_text",
]


def _tool_call_name(tc: Any) -> str:
    return tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")


def _tool_calls_on_message(msg: Any) -> list[Any]:
    if isinstance(msg, ChatMessage):
        return list(msg.tool_calls or []) if msg.role == "assistant" else []
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        return list(msg.tool_calls)
    return []


def _is_tool_result_message(msg: Any) -> bool:
    if isinstance(msg, ChatMessage):
        return msg.role == "tool"
    return getattr(msg, "type", "") == "tool" or type(msg).__name__ == "ToolMessage"


def tool_output_indicates_failure(output: str) -> bool:
    """Delegate failure heuristics to the tool plugin registry (enterprise may override)."""
    from common.plugins.registry import get_registry

    return get_registry().tools.tool_output_indicates_failure(output)


def extract_submit_payload(messages: list[Any]) -> dict[str, Any] | None:
    """Extract _submit call args from messages (from AIMessage-like .tool_calls), not from tool return content."""
    for msg in reversed(messages):
        tool_calls = _tool_calls_on_message(msg)
        if not tool_calls:
            continue
        for tc in tool_calls:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name == "_submit":
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                if isinstance(args, dict) and "result" in args:
                    return args["result"]
                return args if isinstance(args, dict) else {}
    return None


def check_allowlist_in_state(
    messages: list[Any],
    allowed_tool_names: list[str],
    submit_tool_name: str = "_submit",
) -> None:
    """Raise ExecutionStepError if any message has a tool_call for a name not in allowed_tool_names or submit_tool_name."""
    allowed = set(allowed_tool_names) | {submit_tool_name}
    for msg in messages:
        for tc in _tool_calls_on_message(msg):
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name and name not in allowed:
                msg_text = (
                    f"Tool {name!r} not in allowlist. "
                    f"Allowed: {', '.join(sorted(allowed)) or '(none)'}."
                )
                logger.error("Step (governance): %s", msg_text)
                raise ExecutionStepError(
                    msg_text,
                    tool=name,
                    error_details={
                        "error": msg_text,
                        "disallowed_tools": [name],
                        "allowed_tools": sorted(allowed),
                    },
                )


def check_tool_failures_in_state(
    messages: list[Any],
    tool_message_type: str = "tool",
    tool_message_class_name: str = "ToolMessage",
) -> None:
    """Raise ExecutionStepError if any tool message has content that indicates tool failure."""
    for msg in messages:
        is_tool = _is_tool_result_message(msg)
        if not is_tool and (
            getattr(msg, "type", "") == tool_message_type
            or type(msg).__name__ == tool_message_class_name
        ):
            is_tool = True
        if not is_tool:
            continue
        content = msg.content if isinstance(msg, ChatMessage) else getattr(msg, "content", None)
        content = content or ""
        if tool_output_indicates_failure(str(content)):
            logger.error("Tool returned error output in state: %s", str(content)[:500])
            raise ExecutionStepError(
                str(content)[:1000],
                tool=None,
                error_details={"error": str(content)[:2000]},
            )


_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


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


def _try_parse_assistant_json(msg: Any) -> dict[str, Any] | None:
    """Parse assistant message content as JSON object, or wrap prose in summary."""
    content = msg.content if isinstance(msg, ChatMessage) else getattr(msg, "content", None)
    if not content:
        return None
    raw = content if isinstance(content, str) else str(content)
    raw = raw.strip()
    if not raw:
        return None
    parsed = parse_json_object_from_assistant_text(raw)
    if parsed is not None:
        return parsed
    return {"summary": raw[:10000]}


def fallback_output_from_last_assistant_content(
    messages: list[Any],
    assistant_type: str = "ai",
    assistant_class_name: str = "AIMessage",
) -> dict[str, Any]:
    """Use last assistant content when _submit was not called. Prefer JSON parse; else wrap in summary."""
    for msg in reversed(messages):
        is_assistant = isinstance(msg, ChatMessage) and msg.role == "assistant"
        if not is_assistant and (
            getattr(msg, "type", "") == assistant_type or type(msg).__name__ == assistant_class_name
        ):
            is_assistant = True
        if not is_assistant:
            continue
        parsed = _try_parse_assistant_json(msg)
        if parsed is not None:
            return parsed
    raise ExecutionStepError(
        "No _submit call and no assistant content for fallback.",
        error_details={"error": "no_submit_no_content"},
    )
