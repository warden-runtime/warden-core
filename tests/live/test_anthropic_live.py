"""Optional live Anthropic checks (not run in default CI / make tests).

Enable explicitly::

    WARDEN_LIVE_LLM=1 ANTHROPIC_API_KEY=sk-ant-... \\
      uv run --extra worker --extra dev pytest tests/live/test_anthropic_live.py -q -s

Optional model override: ``WARDEN_ANTHROPIC_MODEL`` (default ``claude-haiku-4-5-20251001``).
"""

from __future__ import annotations

import json
import os

import pytest
from common.llm import ChatMessage
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from workers.adapters.simple_schema import FALLBACK_SIMPLE_OUTPUT_SCHEMA
from workers.llm import build_llm
from workers.llm.anthropic import AnthropicChatAdapter
from workers.llm.retrying import RetryingChatModelPort
from workers.llm.structured import SchemaBoundChatModel, invoke_structured_output

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("WARDEN_LIVE_LLM", "").strip().lower() not in {"1", "true", "yes"},
        reason="Set WARDEN_LIVE_LLM=1 (and ANTHROPIC_API_KEY) to run live Anthropic checks",
    ),
    pytest.mark.skipif(
        not (os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
        reason="ANTHROPIC_API_KEY not set",
    ),
]


def _model_name() -> str:
    return os.environ.get("WARDEN_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()


def _api_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _unwrap_anthropic(llm) -> AnthropicChatAdapter:
    if isinstance(llm, RetryingChatModelPort):
        inner = llm._inner
        assert isinstance(inner, AnthropicChatAdapter)
        return inner
    assert isinstance(llm, AnthropicChatAdapter)
    return llm


@pytest.fixture
def anthropic_llm():
    return build_llm(
        provider="anthropic",
        model_name=_model_name(),
        api_key=_api_key(),
    )


@pytest.mark.asyncio
async def test_live_anthropic_ainvoke_content_is_str_or_none(anthropic_llm):
    """Adapter never surfaces raw list content blocks as ChatResponse.content."""
    adapter = _unwrap_anthropic(anthropic_llm)
    underlying = adapter.get_underlying_model()

    messages = [
        ChatMessage(role="system", content="Reply with one short sentence only."),
        ChatMessage(role="human", content="Say hello."),
    ]

    raw = await underlying.ainvoke(
        [
            SystemMessage(content=messages[0].content),
            HumanMessage(content=messages[1].content),
        ]
    )
    assert isinstance(raw, AIMessage)
    # Observe provider shape: LangChain may give str or list blocks.
    print(f"\n[live] raw AIMessage.content type={type(raw.content).__name__!r}")

    response = await adapter.ainvoke(messages)
    assert response.content is None or isinstance(response.content, str)
    assert not isinstance(response.content, list)
    assert response.content  # expect some text for this prompt
    print(f"[live] flattened content={response.content!r}")


@pytest.mark.asyncio
async def test_live_anthropic_bind_json_schema_simple_summary(anthropic_llm):
    """agent-adapter: simple path via bind_json_schema against a live Claude model."""
    bound = anthropic_llm.bind_json_schema(FALLBACK_SIMPLE_OUTPUT_SCHEMA)
    assert isinstance(bound, SchemaBoundChatModel)

    response = await bound.ainvoke(
        [
            ChatMessage(
                role="system",
                content=(
                    "You are a governed workflow agent. "
                    "Return structured output matching the required schema."
                ),
            ),
            ChatMessage(
                role="human",
                content=(
                    "Connectivity check: acknowledge you are reachable "
                    "in the structured summary field."
                ),
            ),
        ]
    )
    assert response.content
    payload = json.loads(response.content)
    assert isinstance(payload.get("summary"), str)
    assert payload["summary"].strip()
    print(f"\n[live] bind_json_schema payload={payload!r}")


@pytest.mark.asyncio
async def test_live_anthropic_invoke_structured_output_summary(anthropic_llm):
    """Same simple fallback schema via invoke_structured_output (tiered native→JSON)."""
    payload = await invoke_structured_output(
        anthropic_llm,
        [
            ChatMessage(
                role="system",
                content="You are a governed workflow agent. Use structured output.",
            ),
            ChatMessage(
                role="human",
                content="Confirm reachability with a brief structured acknowledgment.",
            ),
        ],
        FALLBACK_SIMPLE_OUTPUT_SCHEMA,
    )
    assert payload.get("summary", "").strip()
    print(f"\n[live] invoke_structured_output payload={payload!r}")
