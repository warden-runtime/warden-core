"""Unit tests for MockChatAdapter and name extraction."""

from __future__ import annotations

import json

import pytest
from common.llm import ChatMessage, ToolCall
from workers.llm.mock import (
    MockChatAdapter,
    _human_content_as_text,
    extract_name_from_messages,
)


def test_human_content_as_text_decodes_json_string():
    raw = json.dumps('Greet Grace. Call echo with "hello Grace".')
    assert "Grace" in _human_content_as_text(raw)


def test_human_content_as_text_falls_back_on_invalid_json():
    raw = 'Greet Grace. Call echo with "hello Grace".'
    assert _human_content_as_text(raw) == raw


def test_human_content_as_text_dict_name_fast_path():
    raw = json.dumps({"name": "Grace"})
    assert _human_content_as_text(raw) == "Grace"


def test_extract_name_from_first_human_message():
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(
            role="human",
            content=json.dumps('Greet Ada. Call echo with "hello Ada".'),
        ),
    ]
    assert extract_name_from_messages(messages) == "Ada"


def test_extract_name_turn2_transcript_still_uses_first_human():
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(
            role="human",
            content=json.dumps('Greet Grace. Call echo with "hello Grace".'),
        ),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(name="echo", args={"message": "hello Grace"}, id="1")],
        ),
        ChatMessage(role="tool", content="echo: hello Grace", tool_call_id="1", name="echo"),
    ]
    assert extract_name_from_messages(messages) == "Grace"


@pytest.mark.asyncio
async def test_mock_adapter_turn1_calls_echo():
    adapter = MockChatAdapter()
    response = await adapter.ainvoke(
        [
            ChatMessage(
                role="human",
                content=json.dumps('Greet Ada. Call echo with "hello Ada".'),
            )
        ]
    )
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "echo"
    assert response.tool_calls[0].args["message"] == "hello Ada"


@pytest.mark.asyncio
async def test_mock_adapter_turn2_calls_submit():
    adapter = MockChatAdapter()
    messages = [
        ChatMessage(
            role="human",
            content=json.dumps('Greet Grace. Call echo with "hello Grace".'),
        ),
        ChatMessage(role="tool", content="echo: hello Grace", tool_call_id="1", name="echo"),
    ]
    response = await adapter.ainvoke(messages)
    assert response.tool_calls[0].name == "_submit"
    assert response.tool_calls[0].args["result"]["name"] == "Grace"
    assert response.tool_calls[0].args["result"]["greeting"] == "Hello, Grace!"


@pytest.mark.asyncio
async def test_mock_adapter_summarize_turn1_calls_echo():
    adapter = MockChatAdapter()
    prompt = "Summarize the greeting: Hello, Ada!. Call echo with that greeting text."
    response = await adapter.ainvoke([ChatMessage(role="human", content=json.dumps(prompt))])
    assert response.tool_calls[0].name == "echo"
    assert response.tool_calls[0].args["message"] == "Hello, Ada!"


@pytest.mark.asyncio
async def test_mock_adapter_summarize_turn2_calls_submit():
    adapter = MockChatAdapter()
    prompt = "Summarize the greeting: Hello, Ada!. Call echo with that greeting text."
    messages = [
        ChatMessage(role="human", content=json.dumps(prompt)),
        ChatMessage(role="tool", content="echo: Hello, Ada!", tool_call_id="1", name="echo"),
    ]
    response = await adapter.ainvoke(messages)
    assert response.tool_calls[0].name == "_submit"
    assert response.tool_calls[0].args["result"]["summary"] == "Greeting was: Hello, Ada!"
