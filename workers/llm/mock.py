"""
Context-aware mock chat model for credential-free demos.

Separate from test helpers (_ScriptedLLM); drives a fixed two-turn ReAct script
(echo MCP tool, then _submit) for greet and summarize demo prompts.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Sequence

from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolCall, ToolProtocol

_HELLO_NAME_RE = re.compile(r'hello\s+"?([^".\n]+)"?', re.IGNORECASE)
_GREET_NAME_RE = re.compile(r"Greet\s+(\S+)")
_SUMMARIZE_GREETING_RE = re.compile(
    r"Summarize the greeting:\s*(.+?)(?:\.\s*Call|\.)",
    re.IGNORECASE | re.DOTALL,
)
_DEFAULT_NAME = "Ada"
_SUBMIT_TOOL = "_submit"
_ECHO_TOOL = "echo"


def _human_content_as_text(content: str) -> str:
    stripped = (content or "").strip()
    if not stripped:
        return ""
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, dict):
        name = decoded.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return stripped


def _regex_extract_name(text: str) -> str | None:
    if not text:
        return None
    if match := _HELLO_NAME_RE.search(text):
        return match.group(1).strip()
    if match := _GREET_NAME_RE.search(text):
        return match.group(1).strip()
    return None


def _extract_summarize_greeting(text: str) -> str | None:
    if not text:
        return None
    if match := _SUMMARIZE_GREETING_RE.search(text):
        return match.group(1).strip()
    return None


def _first_human_text(messages: Sequence[ChatMessage]) -> str:
    for msg in messages:
        if msg.role != "human":
            continue
        return _human_content_as_text(msg.content)
    return ""


def extract_name_from_messages(messages: Sequence[ChatMessage]) -> str:
    """Read the saga name from the first human message (resolved prompt)."""
    for msg in messages:
        if msg.role != "human":
            continue
        text = _human_content_as_text(msg.content)
        if name := _regex_extract_name(text):
            return name
        if text.strip():
            return text.strip()
        break
    return _DEFAULT_NAME


def _tool_message_count(messages: Sequence[ChatMessage]) -> int:
    return sum(1 for msg in messages if msg.role == "tool")


class MockStructuredChatAdapter(ChatModelPort):
    """Mock LLM for simple agent-adapter tests: returns schema-shaped JSON content."""

    def __init__(self, schema: dict[str, Any] | None = None) -> None:
        self._schema = schema

    def bind_tools(self, tools: Sequence[ToolProtocol]) -> Self:
        return self

    def bind_json_schema(self, schema: dict[str, Any]) -> MockStructuredChatAdapter:
        return MockStructuredChatAdapter(schema)

    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse:
        human_text = _first_human_text(messages)
        if greeting := _extract_summarize_greeting(human_text):
            payload = {"summary": f"Greeting was: {greeting}"}
        else:
            name = extract_name_from_messages(messages)
            payload = {"summary": f"Hello, {name}!"}
        return ChatResponse(content=json.dumps(payload))


class MockChatAdapter(ChatModelPort):
    """Demo LLM: echo tool on turn 1, _submit on turn 2."""

    def bind_tools(self, tools: Sequence[ToolProtocol]) -> Self:
        return self

    def bind_json_schema(self, schema: dict[str, Any]) -> MockStructuredChatAdapter:
        return MockStructuredChatAdapter(schema)

    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse:
        human_text = _first_human_text(messages)
        tool_count = _tool_message_count(messages)
        if greeting := _extract_summarize_greeting(human_text):
            if tool_count == 0:
                return ChatResponse(
                    tool_calls=[
                        ToolCall(
                            name=_ECHO_TOOL,
                            args={"message": greeting},
                            id=str(uuid.uuid4()),
                        )
                    ]
                )
            return ChatResponse(
                tool_calls=[
                    ToolCall(
                        name=_SUBMIT_TOOL,
                        args={
                            "result": {
                                "summary": f"Greeting was: {greeting}",
                            }
                        },
                        id=str(uuid.uuid4()),
                    )
                ]
            )
        name = extract_name_from_messages(messages)
        if tool_count == 0:
            return ChatResponse(
                tool_calls=[
                    ToolCall(
                        name=_ECHO_TOOL,
                        args={"message": f"hello {name}"},
                        id=str(uuid.uuid4()),
                    )
                ]
            )
        return ChatResponse(
            tool_calls=[
                ToolCall(
                    name=_SUBMIT_TOOL,
                    args={
                        "result": {
                            "greeting": f"Hello, {name}!",
                            "name": name,
                        }
                    },
                    id=str(uuid.uuid4()),
                )
            ]
        )
