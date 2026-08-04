"""Optional live Azure OpenAI / Foundry checks (not run in default CI / make tests).

Enable explicitly::

    WARDEN_LIVE_LLM=1 AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=https://....openai.azure.com/ \\
      uv run --extra worker --extra dev pytest tests/live/test_azure_live.py -q -s

Optional deployment override: ``WARDEN_AZURE_MODEL`` (default ``gpt-5.4-mini-1``).
"""

from __future__ import annotations

import json
import os

import pytest
from common.llm import ChatMessage
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from workers.adapters.simple_schema import FALLBACK_SIMPLE_OUTPUT_SCHEMA
from workers.llm import build_llm
from workers.llm.azure import AzureChatAdapter
from workers.llm.retrying import RetryingChatModelPort
from workers.llm.structured import SchemaBoundChatModel, invoke_structured_output

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("WARDEN_LIVE_LLM", "").strip().lower() not in {"1", "true", "yes"},
        reason="Set WARDEN_LIVE_LLM=1 (and Azure credentials) to run live Azure checks",
    ),
    pytest.mark.skipif(
        not (os.environ.get("AZURE_OPENAI_API_KEY") or "").strip(),
        reason="AZURE_OPENAI_API_KEY not set",
    ),
    pytest.mark.skipif(
        not (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip(),
        reason="AZURE_OPENAI_ENDPOINT not set",
    ),
]


def _model_name() -> str:
    return os.environ.get("WARDEN_AZURE_MODEL", "gpt-5.4-mini-1").strip()


def _api_key() -> str:
    return (os.environ.get("AZURE_OPENAI_API_KEY") or "").strip()


def _unwrap_azure(llm) -> AzureChatAdapter:
    if isinstance(llm, RetryingChatModelPort):
        inner = llm._inner
        assert isinstance(inner, AzureChatAdapter)
        return inner
    assert isinstance(llm, AzureChatAdapter)
    return llm


@pytest.fixture
def azure_llm():
    return build_llm(
        provider="azure",
        model_name=_model_name(),
        api_key=_api_key(),
    )


@pytest.mark.asyncio
async def test_live_azure_ainvoke_content_is_str_or_none(azure_llm):
    """Adapter never surfaces raw list content blocks as ChatResponse.content."""
    adapter = _unwrap_azure(azure_llm)
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
async def test_live_azure_bind_json_schema_simple_summary(azure_llm):
    """agent-adapter: simple path via bind_json_schema against a live Azure deployment."""
    bound = azure_llm.bind_json_schema(FALLBACK_SIMPLE_OUTPUT_SCHEMA)
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
async def test_live_azure_invoke_structured_output_summary(azure_llm):
    """Same simple fallback schema via invoke_structured_output (tiered native→JSON)."""
    payload = await invoke_structured_output(
        azure_llm,
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
