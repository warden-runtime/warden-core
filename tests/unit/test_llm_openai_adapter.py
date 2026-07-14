"""Unit tests for the OpenAI chat adapter."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from common.llm import ChatMessage, ChatResponse
from langchain_core.messages import AIMessage
from workers.llm.openai import OpenAIChatAdapter


@pytest.fixture
def mock_llm():
    """Mock ChatOpenAI instance."""
    return MagicMock()


@pytest.fixture
def adapter(mock_llm):
    """OpenAIChatAdapter with mocked underlying LLM."""
    return OpenAIChatAdapter(
        model_name="gpt-4o",
        api_key="sk-fake",
        _llm=mock_llm,
    )


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
async def test_ainvoke_flattens_list_content_blocks(adapter, mock_llm):
    """OpenAI-compatible list content blocks are flattened (e.g. local servers)."""
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=[
                {"type": "text", "text": '{"summary": "ok"}'},
                {"type": "text", "text": " more"},
            ],
            tool_calls=[],
        ),
    )
    response = await adapter.ainvoke([ChatMessage(role="human", content="Go")])
    assert response.content == '{"summary": "ok"}\n more'


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
    """bind_tools returns a new OpenAIChatAdapter with bound LLM."""
    mock_tool = MagicMock()
    mock_tool.name = "test_tool"
    mock_tool.ainvoke = AsyncMock(return_value="ok")
    mock_llm.bind_tools = MagicMock(return_value=MagicMock())
    bound = adapter.bind_tools([mock_tool])
    assert isinstance(bound, OpenAIChatAdapter)
    assert bound is not adapter
    mock_llm.bind_tools.assert_called_once()
