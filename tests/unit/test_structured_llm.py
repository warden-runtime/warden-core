"""Tiered structured LLM completion."""

import pytest
from common.agent_adapter import ExecutionStepError
from common.execution_timing import WorkerTimingAccumulator
from common.llm import ChatMessage, ChatModelPort, ChatResponse
from workers.adapters.simple_schema import FALLBACK_SIMPLE_OUTPUT_SCHEMA, resolve_effective_schema
from workers.adapters.state_utils import parse_json_object_from_assistant_text
from workers.llm.structured import invoke_structured_output


class _JsonContentLLM(ChatModelPort):
    def __init__(self, content: str) -> None:
        self._content = content

    def bind_tools(self, tools):
        return self

    def bind_json_schema(self, schema):
        return self

    def get_underlying_model(self):
        return None

    async def ainvoke(self, messages):
        return ChatResponse(content=self._content)


def test_resolve_effective_schema_uses_fallback():
    assert resolve_effective_schema(None) == FALLBACK_SIMPLE_OUTPUT_SCHEMA


def test_parse_json_object_strips_markdown_fence():
    raw = '```json\n{"summary": "ok"}\n```'
    assert parse_json_object_from_assistant_text(raw) == {"summary": "ok"}


def test_parse_json_object_extracts_object_from_preamble():
    raw = 'Here is the result:\n{"summary": "done"}'
    assert parse_json_object_from_assistant_text(raw) == {"summary": "done"}


@pytest.mark.asyncio
async def test_invoke_structured_output_json_mode_success():
    llm = _JsonContentLLM('{"summary": "hello"}')
    payload = await invoke_structured_output(
        llm,
        [ChatMessage(role="human", content="go")],
        FALLBACK_SIMPLE_OUTPUT_SCHEMA,
    )
    assert payload == {"summary": "hello"}


@pytest.mark.asyncio
async def test_invoke_structured_output_records_llm_ms():
    llm = _JsonContentLLM('{"summary": "hello"}')
    timing = WorkerTimingAccumulator()
    await invoke_structured_output(
        llm,
        [ChatMessage(role="human", content="go")],
        FALLBACK_SIMPLE_OUTPUT_SCHEMA,
        timing_acc=timing,
    )
    wire = timing.to_wire() or {}
    assert wire.get("worker", {}).get("llm_ms", 0) >= 0


@pytest.mark.asyncio
async def test_invoke_structured_output_fails_on_unparseable_content():
    llm = _JsonContentLLM("not json at all")
    with pytest.raises(ExecutionStepError) as exc_info:
        await invoke_structured_output(
            llm,
            [ChatMessage(role="human", content="go")],
            FALLBACK_SIMPLE_OUTPUT_SCHEMA,
        )
    assert exc_info.value.error_details.get("code") == "structured_output_failed"
    assert exc_info.value.error_details.get("message")
