"""Unit tests for RetryingChatModelPort."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from common.llm import ChatMessage, ChatResponse
from workers.llm.retrying import LlmRetryPolicy, RetryingChatModelPort


class _FlakyLLM:
    def __init__(self) -> None:
        self.ainvoke_calls = 0

    def bind_tools(self, tools: object) -> _FlakyLLM:
        clone = _FlakyLLM()
        clone.bound_tools = tools
        return clone

    def get_underlying_model(self) -> None:
        return None

    async def ainvoke(self, messages: list[ChatMessage]) -> ChatResponse:
        self.ainvoke_calls += 1
        if self.ainvoke_calls < 3:
            raise ConnectionError("transient")
        return ChatResponse(content="done")


@pytest.mark.asyncio
async def test_retrying_port_ainvoke_retries_then_succeeds():
    inner = _FlakyLLM()
    policy = LlmRetryPolicy(max_attempts=3, base_delay_s=0.01, max_delay_s=0.02)
    llm = RetryingChatModelPort(inner, policy)
    with patch("common.retry.asyncio.sleep", new_callable=AsyncMock):
        response = await llm.ainvoke([ChatMessage(role="human", content="hi")])
    assert response.content == "done"
    assert inner.ainvoke_calls == 3


@pytest.mark.asyncio
async def test_retrying_port_exhausts_attempts():
    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=ConnectionError("still down"))
    inner.get_underlying_model = MagicMock(return_value=None)
    policy = LlmRetryPolicy(max_attempts=2, base_delay_s=0.01, max_delay_s=0.02)
    llm = RetryingChatModelPort(inner, policy)
    with patch("common.retry.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ConnectionError, match="still down"):
            await llm.ainvoke([ChatMessage(role="human", content="hi")])
    assert inner.ainvoke.await_count == 2


def test_bind_tools_returns_retrying_wrapper():
    inner = MagicMock()
    bound_inner = MagicMock()
    inner.bind_tools = MagicMock(return_value=bound_inner)
    inner.get_underlying_model = MagicMock(return_value="model")
    policy = LlmRetryPolicy(max_attempts=3, base_delay_s=1.0, max_delay_s=60.0)
    llm = RetryingChatModelPort(inner, policy)
    tools = [MagicMock(name="tool")]

    wrapped = llm.bind_tools(tools)

    assert isinstance(wrapped, RetryingChatModelPort)
    assert wrapped is not llm
    assert wrapped._inner is bound_inner
    assert wrapped._policy == policy
    inner.bind_tools.assert_called_once_with(tools)
