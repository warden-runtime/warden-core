"""Shared LangChain message helpers for provider adapters."""

from __future__ import annotations

from typing import Any

from common.llm import ChatMessage, ChatResponse, ToolCall
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


def aimessage_to_chat_response(aimessage: AIMessage) -> ChatResponse:
    """Convert a LangChain AIMessage to ChatResponse."""
    tool_calls = [_tool_call_from_lc(tc) for tc in getattr(aimessage, "tool_calls", []) or []]
    return ChatResponse(
        content=flatten_aimessage_content(aimessage.content),
        tool_calls=tool_calls,
    )
