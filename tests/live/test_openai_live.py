"""Optional live OpenAI checks (not run in default CI / make tests).

Enable explicitly::

    WARDEN_LIVE_LLM=1 OPENAI_API_KEY=sk-... \\
      uv run --extra worker --extra dev pytest tests/live/test_openai_live.py -q -s

Optional model override: ``WARDEN_OPENAI_MODEL`` (default ``gpt-4o-mini``).
"""

from __future__ import annotations

import json
import os

import pytest
from common.llm import ChatMessage
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from workers.adapters.simple_schema import FALLBACK_SIMPLE_OUTPUT_SCHEMA
from workers.llm import build_llm
from workers.llm.openai import OpenAIChatAdapter
from workers.llm.retrying import RetryingChatModelPort
from workers.llm.structured import SchemaBoundChatModel, invoke_structured_output

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("WARDEN_LIVE_LLM", "").strip().lower() not in {"1", "true", "yes"},
        reason="Set WARDEN_LIVE_LLM=1 (and OPENAI_API_KEY) to run live OpenAI checks",
    ),
    pytest.mark.skipif(
        not (os.environ.get("OPENAI_API_KEY") or "").strip(),
        reason="OPENAI_API_KEY not set",
    ),
]


def _model_name() -> str:
    return os.environ.get("WARDEN_OPENAI_MODEL", "gpt-4o-mini").strip()


def _api_key() -> str:
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def _unwrap_openai(llm) -> OpenAIChatAdapter:
    if isinstance(llm, RetryingChatModelPort):
        inner = llm._inner
        assert isinstance(inner, OpenAIChatAdapter)
        return inner
    assert isinstance(llm, OpenAIChatAdapter)
    return llm


@pytest.fixture
def openai_llm():
    return build_llm(
        provider="openai",
        model_name=_model_name(),
        api_key=_api_key(),
    )


@pytest.mark.asyncio
async def test_live_openai_ainvoke_content_is_str_or_none(openai_llm):
    """Adapter never surfaces raw list content blocks as ChatResponse.content."""
    adapter = _unwrap_openai(openai_llm)
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
    print(f"\n[live] raw AIMessage.content type={type(raw.content).__name__!r}")

    response = await adapter.ainvoke(messages)
    assert response.content is None or isinstance(response.content, str)
    assert not isinstance(response.content, list)
    assert response.content  # expect some text for this prompt
    print(f"[live] flattened content={response.content!r}")


@pytest.mark.asyncio
async def test_live_openai_bind_json_schema_simple_summary(openai_llm):
    """agent-adapter: simple path via bind_json_schema against a live OpenAI model."""
    bound = openai_llm.bind_json_schema(FALLBACK_SIMPLE_OUTPUT_SCHEMA)
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
async def test_live_openai_invoke_structured_output_summary(openai_llm):
    """Same simple fallback schema via invoke_structured_output (tiered native→JSON)."""
    payload = await invoke_structured_output(
        openai_llm,
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
