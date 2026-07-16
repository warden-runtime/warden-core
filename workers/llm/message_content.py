"""Shared LangChain message helpers for provider adapters."""

from __future__ import annotations

from typing import Any

from common.execution_timing import clamp_nonneg
from common.execution_usage import normalize_usage_details
from common.llm import ChatMessage, ChatResponse, TokenUsage, ToolCall
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


def chat_message_to_langchain(msg: ChatMessage) -> BaseMessage:
    """Convert a ChatMessage to the corresponding LangChain message type."""
    if msg.role == "system":
        return SystemMessage(content=msg.content)
    if msg.role == "human":
        return HumanMessage(content=msg.content)
    if msg.role == "assistant":
        lc_tool_calls = None
        if msg.tool_calls:
            lc_tool_calls = [
                {"name": tc.name, "args": tc.args, "id": tc.id} for tc in msg.tool_calls
            ]
        return AIMessage(content=msg.content, tool_calls=lc_tool_calls or [])
    if msg.role == "tool":
        return ToolMessage(
            content=msg.content,
            tool_call_id=msg.tool_call_id or "",
            name=msg.name or "",
        )
    raise ValueError(f"Unknown ChatMessage role: {msg.role!r}")


def _nonempty_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _text_from_content_block(block: Any) -> str | None:
    """Extract text from one LangChain/provider content block; ignore non-text."""
    if isinstance(block, str):
        return _nonempty_text(block)
    if isinstance(block, dict):
        if block.get("type", "text") != "text":
            return None
        return _nonempty_text(block.get("text"))
    if getattr(block, "type", "text") not in (None, "text"):
        return None
    return _nonempty_text(getattr(block, "text", None))


def flatten_aimessage_content(content: Any) -> str | None:
    """Normalize LangChain AIMessage content (str or block list) to a single string.

    Providers (notably Anthropic, some OpenAI-compatible local servers) may return a
    list of content blocks (``text``, ``tool_use``, …). Non-text blocks are ignored
    here; LangChain already surfaces tool_use as ``tool_calls``. Empty /
    whitespace-only text becomes ``None``.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts = [text for block in content if (text := _text_from_content_block(block))]
    return "\n".join(parts).strip() or None


def _tool_call_from_lc(tc: Any) -> ToolCall:
    if hasattr(tc, "get"):
        return ToolCall(
            name=tc.get("name", ""),
            args=tc.get("args") or {},
            id=tc.get("id") or "",
        )
    return ToolCall(
        name=getattr(tc, "name", ""),
        args=getattr(tc, "args", None) or {},
        id=getattr(tc, "id", "") or "",
    )


def _int_or_zero(value: Any) -> int:
    try:
        return clamp_nonneg(int(value))
    except (TypeError, ValueError):
        return 0


def _model_id_from_response_metadata(meta: Any) -> str | None:
    if not isinstance(meta, dict):
        return None
    for key in ("model", "model_name", "model_id"):
        raw = meta.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _details_from_usage_metadata(usage_meta: dict[str, Any]) -> dict[str, int]:
    details: dict[str, int] = {}
    for key in ("input_token_details", "output_token_details", "input_details", "output_details"):
        blob = usage_meta.get(key)
        if isinstance(blob, dict):
            for dk, dv in normalize_usage_details(blob).items():
                details[dk] = details.get(dk, 0) + dv
    # Some providers put cache/reasoning keys at the top level of usage_metadata.
    top_level = {
        k: v
        for k, v in usage_meta.items()
        if k
        not in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "input_token_details",
            "output_token_details",
            "input_details",
            "output_details",
        )
    }
    for dk, dv in normalize_usage_details(top_level).items():
        details[dk] = details.get(dk, 0) + dv
    return {k: v for k, v in details.items() if v > 0}


def _counts_from_usage_metadata(meta: dict[str, Any]) -> tuple[int, int, int]:
    prompt = _int_or_zero(meta.get("input_tokens"))
    completion = _int_or_zero(meta.get("output_tokens"))
    total = _int_or_zero(meta.get("total_tokens"))
    if total <= 0 and (prompt or completion):
        total = prompt + completion
    return prompt, completion, total


def token_usage_from_aimessage(aimessage: AIMessage) -> TokenUsage | None:
    """Extract provider-reported usage from a LangChain AIMessage, if present."""
    usage_meta = getattr(aimessage, "usage_metadata", None)
    meta = usage_meta if isinstance(usage_meta, dict) else {}
    prompt, completion, total = _counts_from_usage_metadata(meta)
    details = _details_from_usage_metadata(meta) if meta else {}
    model_id = _model_id_from_response_metadata(getattr(aimessage, "response_metadata", None))
    if not (prompt or completion or total or details or model_id):
        return None
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        model_id=model_id,
        details=details,
    )


def aimessage_to_chat_response(aimessage: AIMessage) -> ChatResponse:
    """Convert a LangChain AIMessage to ChatResponse."""
    tool_calls = [_tool_call_from_lc(tc) for tc in getattr(aimessage, "tool_calls", []) or []]
    return ChatResponse(
        content=flatten_aimessage_content(aimessage.content),
        tool_calls=tool_calls,
        usage=token_usage_from_aimessage(aimessage),
    )
