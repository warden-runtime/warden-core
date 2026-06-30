"""Smoke tests for common.llm protocol and DTOs."""

import pytest
from common.llm import ChatMessage, ChatResponse, ToolCall, ToolProtocol


class StubTool:
    """Minimal stub satisfying ToolProtocol (structural typing)."""

    name = "stub_tool"

    async def ainvoke(self, args: dict):
        return "ok"


def test_tool_protocol_satisfied_by_stub():
    """A minimal stub with name and ainvoke satisfies ToolProtocol."""
    stub = StubTool()
    assert stub.name == "stub_tool"

    # Structural: we can pass it where ToolProtocol is expected
    def accept_tool(t: ToolProtocol) -> str:
        return t.name

    assert accept_tool(stub) == "stub_tool"


@pytest.mark.asyncio
async def test_stub_tool_ainvoke():
    """Stub ainvoke is callable."""
    stub = StubTool()
    out = await stub.ainvoke({})
    assert out == "ok"


def test_chat_message_serialize_roundtrip():
    """ChatMessage validates and serializes."""
    msg = ChatMessage(role="human", content="hello")
    assert msg.role == "human"
    assert msg.content == "hello"
    dumped = msg.model_dump()
    assert dumped["role"] == "human"
    assert ChatMessage.model_validate(dumped) == msg


def test_chat_message_tool_role_optional_fields():
    """ChatMessage with role=tool accepts tool_call_id and name."""
    msg = ChatMessage(
        role="tool",
        content="result",
        tool_call_id="call_1",
        name="my_tool",
    )
    assert msg.tool_call_id == "call_1"
    assert msg.name == "my_tool"


def test_tool_call_serialize_roundtrip():
    """ToolCall validates and serializes."""
    tc = ToolCall(name="foo", args={"a": 1}, id="id-1")
    assert tc.name == "foo"
    assert tc.args == {"a": 1}
    assert ToolCall.model_validate(tc.model_dump()) == tc


def test_chat_response_serialize_roundtrip():
    """ChatResponse validates and serializes."""
    resp = ChatResponse(
        content="done",
        tool_calls=[ToolCall(name="t", args={}, id="1")],
    )
    assert resp.content == "done"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "t"
    assert ChatResponse.model_validate(resp.model_dump()).content == resp.content
