"""Shared step failure detail builders and CLI brief formatting."""

from __future__ import annotations

from typing import Any


def build_step_error_details(
    *,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    """Normalize worker/engine failure payloads with a stable code + message shape."""
    details: dict[str, Any] = {"code": code, "message": message}
    details.update(extra)
    return details


def _error_brief_code(error_details: dict[str, Any]) -> str:
    return str(error_details.get("code") or error_details.get("error") or "UNKNOWN_ERROR")


def _append_preview(brief: str, preview: Any, *, preview_len: int) -> str:
    if not preview:
        return brief
    return f"{brief} ({str(preview)[:preview_len]})"


def _brief_from_last_assistant_content(
    error_details: dict[str, Any],
    *,
    preview_len: int,
) -> str | None:
    if _error_brief_code(error_details) != "no_submit_call":
        return None
    content = error_details.get("last_assistant_content")
    if not content:
        return None
    return _append_preview(_error_brief_code(error_details), content, preview_len=preview_len)


def _brief_from_tool_errors(error_details: dict[str, Any], *, preview_len: int) -> str | None:
    tool_errors = error_details.get("last_tool_errors")
    if not isinstance(tool_errors, list) or not tool_errors:
        return None
    first = tool_errors[0] if isinstance(tool_errors[0], dict) else {}
    preview = first.get("preview") or first.get("tool")
    if not preview:
        return None
    return _append_preview(_error_brief_code(error_details), preview, preview_len=preview_len)


def _brief_from_message_keys(error_details: dict[str, Any], *, preview_len: int) -> str:
    code = _error_brief_code(error_details)
    for key in ("message", "error"):
        text = error_details.get(key)
        if text is not None and str(text) != code:
            return _append_preview(code, text, preview_len=preview_len)
    return code


def _brief_from_source_failures(error_details: dict[str, Any], *, preview_len: int) -> str | None:
    code = _error_brief_code(error_details)
    if code != "MCP_UNAVAILABLE":
        return None
    failures = error_details.get("source_failures")
    if not isinstance(failures, list) or not failures:
        return None
    first = failures[0] if isinstance(failures[0], dict) else {}
    detail = first.get("error")
    if not detail:
        return None
    return _append_preview(code, detail, preview_len=preview_len)


def format_step_error_brief(
    error_details: dict[str, Any] | None,
    *,
    preview_len: int = 60,
) -> str:
    """One-line failure summary for CLI list --errors and show step."""
    if not error_details:
        return ""
    from_assistant = _brief_from_last_assistant_content(error_details, preview_len=preview_len)
    if from_assistant is not None:
        return from_assistant
    from_tools = _brief_from_tool_errors(error_details, preview_len=preview_len)
    if from_tools is not None:
        return from_tools
    from_sources = _brief_from_source_failures(error_details, preview_len=preview_len)
    if from_sources is not None:
        return from_sources
    for preview_key in ("tool_result_preview", "response_preview"):
        preview = error_details.get(preview_key)
        if preview:
            return _append_preview(
                _error_brief_code(error_details),
                preview,
                preview_len=preview_len,
            )
    return _brief_from_message_keys(error_details, preview_len=preview_len)


__all__ = ["build_step_error_details", "format_step_error_brief"]
