"""Unit tests for the Anthropic chat adapter."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from common.llm import ChatMessage, ChatResponse
from langchain_core.messages import AIMessage
from workers.adapters.simple_schema import FALLBACK_SIMPLE_OUTPUT_SCHEMA
from workers.llm.anthropic import AnthropicChatAdapter
from workers.llm.structured import SchemaBoundChatModel, invoke_structured_output


@pytest.fixture
def mock_llm():
    """Mock ChatAnthropic instance."""
    return MagicMock()


@pytest.fixture
def adapter(mock_llm):
    """AnthropicChatAdapter with mocked underlying LLM."""
    return AnthropicChatAdapter(
        model_name="claude-3-5-sonnet-20241022",
        api_key="sk-ant-fake",
        _llm=mock_llm,
    )


def _force_json_mode_fallback(mock_llm: MagicMock) -> None:
    """Make native with_structured_output fail so simple falls back to llm.ainvoke."""
    mock_llm.with_structured_output.side_effect = RuntimeError("native structured unavailable")


@pytest.mark.asyncio
async def test_ainvoke_returns_chat_response_with_content(adapter, mock_llm):
    """Adapter ainvoke returns ChatResponse with content when LLM returns text."""
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(content="Hello", tool_calls=[]),
    )
    messages = [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="human", content="Hi"),
    ]
    response = await adapter.ainvoke(messages)
    assert isinstance(response, ChatResponse)
    assert response.content == "Hello"
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_ainvoke_flattens_content_blocks(adapter, mock_llm):
    """Anthropic list content blocks are flattened into a single string."""
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=[
                {"type": "text", "text": "Part one."},
                {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}},
                {"type": "text", "text": "Part two."},
            ],
            tool_calls=[
                {"name": "lookup", "args": {}, "id": "tool_1"},
            ],
        ),
    )
    response = await adapter.ainvoke([ChatMessage(role="human", content="Go")])
    assert response.content == "Part one.\nPart two."
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "lookup"


@pytest.mark.asyncio
async def test_ainvoke_returns_tool_calls(adapter, mock_llm):
    """Adapter ainvoke returns ChatResponse with tool_calls when LLM returns tool calls."""
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[
                {"name": "get_weather", "args": {"city": "Paris"}, "id": "call_1"},
            ],
        ),
    )
    messages = [
        ChatMessage(role="human", content="What's the weather in Paris?"),
    ]
    response = await adapter.ainvoke(messages)
    assert isinstance(response, ChatResponse)
    assert response.content is None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "get_weather"
    assert response.tool_calls[0].args == {"city": "Paris"}
    assert response.tool_calls[0].id == "call_1"


@pytest.mark.asyncio
async def test_ainvoke_converts_messages_to_llm(adapter, mock_llm):
    """Adapter passes converted LangChain messages to the LLM."""
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(content="Ok", tool_calls=[]),
    )
    messages = [
        ChatMessage(role="system", content="Sys"),
        ChatMessage(role="human", content="User"),
    ]
    await adapter.ainvoke(messages)
    mock_llm.ainvoke.assert_awaited_once()
    call_args = mock_llm.ainvoke.call_args[0][0]
    assert len(call_args) == 2
    assert call_args[0].__class__.__name__ == "SystemMessage"
    assert call_args[1].__class__.__name__ == "HumanMessage"


@pytest.mark.asyncio
async def test_ainvoke_logs_and_reraises_on_failure(adapter, mock_llm):
    """Adapter logs and re-raises when LLM raises."""
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API error"))
    messages = [ChatMessage(role="human", content="Hi")]
    with pytest.raises(RuntimeError, match="API error"):
        await adapter.ainvoke(messages)


def test_bind_tools_returns_new_adapter(adapter, mock_llm):
    """bind_tools returns a new AnthropicChatAdapter with bound LLM."""
    mock_tool = MagicMock()
    mock_tool.name = "test_tool"
    mock_tool.ainvoke = AsyncMock(return_value="ok")
    mock_llm.bind_tools = MagicMock(return_value=MagicMock())
    bound = adapter.bind_tools([mock_tool])
    assert isinstance(bound, AnthropicChatAdapter)
    assert bound is not adapter
    mock_llm.bind_tools.assert_called_once()


def test_bind_json_schema_returns_schema_bound_wrapper(adapter):
    """Anthropic bind_json_schema returns the shared SchemaBoundChatModel wrapper."""
    bound = adapter.bind_json_schema(FALLBACK_SIMPLE_OUTPUT_SCHEMA)
    assert isinstance(bound, SchemaBoundChatModel)
    assert bound is not adapter


@pytest.mark.asyncio
async def test_bind_json_schema_json_mode_uses_flattened_list_content(adapter, mock_llm):
    """simple path: native structured fails; JSON-mode needs list-block flattening.

    Without flattening, list content becomes ChatResponse.content=None and
    invoke_structured_output cannot parse JSON.
    """
    _force_json_mode_fallback(mock_llm)
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=[
                {
                    "type": "text",
                    "text": '{"summary": "reachable via flattened blocks"}',
                },
                {"type": "tool_use", "id": "ignored", "name": "noop", "input": {}},
            ],
            tool_calls=[],
        ),
    )

    bound = adapter.bind_json_schema(FALLBACK_SIMPLE_OUTPUT_SCHEMA)
    response = await bound.ainvoke([ChatMessage(role="human", content="go")])

    assert isinstance(response, ChatResponse)
    assert response.content is not None
    assert "reachable via flattened blocks" in response.content
    mock_llm.ainvoke.assert_awaited()
    mock_llm.with_structured_output.assert_called()


@pytest.mark.asyncio
async def test_invoke_structured_output_json_mode_with_anthropic_list_blocks(adapter, mock_llm):
    """invoke_structured_output JSON fallback parses flattened Anthropic blocks."""
    _force_json_mode_fallback(mock_llm)
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=[{"type": "text", "text": '{"summary": "from blocks"}'}],
            tool_calls=[],
        ),
    )

    payload = await invoke_structured_output(
        adapter,
        [ChatMessage(role="human", content="go")],
        FALLBACK_SIMPLE_OUTPUT_SCHEMA,
    )
    assert payload == {"summary": "from blocks"}
