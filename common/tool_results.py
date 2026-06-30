"""Shared constants and helpers for MCP tool result text (kernel-safe)."""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_TOOL_RESULT_RECORD_LIMIT = 8000
"""Historical worker cap on recorded tool text; used for truncation detection only."""

DEFAULT_TOOL_MESSAGE_LIMIT = 8000
"""Default max chars for tool-role ChatMessage content in ReAct loops."""

_LLM_TRUNCATION_SUFFIX = "\n...[WARDEN: tool output truncated for LLM context]"
_WARDEN_CLIPPED_KEY = "_warden_clipped"
_MAX_TOP_LEVEL_STRING_LEN = 512
_INITIAL_LIST_KEEP = 5


def resolve_tool_message_limit(env_value: int | None = None) -> int | None:
    """Return clip limit for LLM tool messages; None when clipping is disabled."""
    if env_value is not None:
        return None if env_value <= 0 else env_value
    raw = os.environ.get("WARDEN_REACT_TOOL_MESSAGE_LIMIT", str(DEFAULT_TOOL_MESSAGE_LIMIT))
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_TOOL_MESSAGE_LIMIT
    return None if parsed <= 0 else parsed


def tool_message_limit_from_env() -> int | None:
    """Resolve clip limit from ``WARDEN_REACT_TOOL_MESSAGE_LIMIT``."""
    return resolve_tool_message_limit()


def clip_tool_text_for_llm(text: str, *, limit: int) -> str:
    """Return text suitable for ChatMessage(role=tool). Facts path uses raw text."""
    if limit <= 0 or len(text) <= limit:
        return text
    clipped = _try_clip_json_object_for_limit(text, limit=limit)
    if clipped is not None:
        return clipped
    return _plain_truncate_for_llm(text, limit=limit)


def _plain_truncate_for_llm(text: str, *, limit: int) -> str:
    suffix = _LLM_TRUNCATION_SUFFIX
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


def _try_clip_json_object_for_limit(text: str, *, limit: int) -> str | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    clipped_doc = _clip_json_dict_for_limit(parsed, limit=limit)
    if clipped_doc is None:
        return None
    return json.dumps(clipped_doc, ensure_ascii=False)


def _clip_json_dict_for_limit(document: dict[str, Any], *, limit: int) -> dict[str, Any] | None:
    trimmed = _truncate_top_level_strings(document, max_len=_MAX_TOP_LEVEL_STRING_LEN)
    strings_changed = trimmed is not document
    keep_values = (
        range(_INITIAL_LIST_KEEP, -1, -1) if _has_non_empty_top_level_list(trimmed) else (0,)
    )
    for keep in keep_values:
        candidate = _slice_top_level_lists(trimmed, keep=keep)
        lists_changed = _top_level_lists_exceed_keep(trimmed, keep=keep)
        if strings_changed or lists_changed:
            candidate = dict(candidate)
            candidate[_WARDEN_CLIPPED_KEY] = True
        serialized = json.dumps(candidate, ensure_ascii=False)
        if len(serialized) <= limit:
            return candidate
    return None


def _has_non_empty_top_level_list(document: dict[str, Any]) -> bool:
    return any(isinstance(value, list) and len(value) > 0 for value in document.values())


def _top_level_lists_exceed_keep(document: dict[str, Any], *, keep: int) -> bool:
    safe_keep = max(0, keep)
    for value in document.values():
        if isinstance(value, list) and len(value) > safe_keep:
            return True
    return False


def _truncate_top_level_strings(document: dict[str, Any], *, max_len: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    changed = False
    for key, value in document.items():
        if isinstance(value, str) and len(value) > max_len:
            out[key] = value[:max_len]
            changed = True
        else:
            out[key] = value
    return out if changed else document


def _slice_top_level_lists(document: dict[str, Any], *, keep: int) -> dict[str, Any]:
    safe_keep = max(0, keep)
    out: dict[str, Any] = {}
    for key, value in document.items():
        if isinstance(value, list):
            # Empty lists stay empty; slicing never indexes into elements.
            out[key] = [] if not value else value[:safe_keep]
        else:
            out[key] = value
    return out


__all__ = [
    "DEFAULT_TOOL_MESSAGE_LIMIT",
    "DEFAULT_TOOL_RESULT_RECORD_LIMIT",
    "clip_tool_text_for_llm",
    "resolve_tool_message_limit",
    "tool_message_limit_from_env",
]
