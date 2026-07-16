"""Unit tests for ReAct OpenInference-style child spans."""

from __future__ import annotations

import pytest
from common.execution_usage import WorkerUsageAccumulator
from common.llm import ChatMessage, ChatResponse, TokenUsage, ToolCall
from common.telemetry import log_context
from workers.adapters.react_loop import run_react_loop


class _ScriptedLLM:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if not self._responses:
            raise AssertionError("unexpected LLM call")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_react_loop_emits_llm_and_tool_child_spans(memory_span_exporter):
    memory_span_exporter.clear()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content=None,
                tool_calls=[ToolCall(name="echo", args={"x": 1}, id="c1")],
            ),
            ChatResponse(
                content=None,
                tool_calls=[ToolCall(name="_submit", args={"result": {"ok": True}}, id="c2")],
            ),
        ]
    )

    async def _echo(args: dict):
        return f"echoed:{args.get('x')}"

    tool = type("T", (), {"name": "echo", "ainvoke": staticmethod(_echo)})()

    with log_context(trace_id="trace-react", span_id="span-react", step_id="greet"):
        result = await run_react_loop(
            llm=llm,
            initial_messages=[
                ChatMessage(role="system", content="sys"),
                ChatMessage(role="human", content="go"),
            ],
            mcp_tools=[tool],
            allowed_tool_names=["echo"],
            completion_mode="submit",
            max_turns=3,
        )

    assert result.submit_payload == {"ok": True}
    spans = memory_span_exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert any(n.startswith("react.llm.turn_") for n in names)
    assert "react.tool.echo" in names
    tool_span = next(s for s in spans if s.name == "react.tool.echo")
    assert tool_span.attributes["openinference.span.kind"] == "TOOL"
    assert tool_span.attributes["saga.id"] == "trace-react"
    assert tool_span.attributes["saga.step_span_id"] == "span-react"
    assert tool_span.attributes["saga.step_id"] == "greet"
    assert tool_span.attributes["tool.name"] == "echo"


@pytest.mark.asyncio
async def test_react_loop_records_token_usage_on_llm_spans(memory_span_exporter):
    memory_span_exporter.clear()
    usage_acc = WorkerUsageAccumulator()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content=None,
                tool_calls=[ToolCall(name="_submit", args={"result": {"ok": True}}, id="c1")],
                usage=TokenUsage(
                    prompt_tokens=40,
                    completion_tokens=8,
                    total_tokens=48,
                    model_id="mock-model",
                ),
            ),
        ]
    )

    result = await run_react_loop(
        llm=llm,
        initial_messages=[
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="human", content="go"),
        ],
        mcp_tools=[],
        allowed_tool_names=[],
        completion_mode="submit",
        max_turns=2,
        usage_acc=usage_acc,
    )

    assert result.submit_payload == {"ok": True}
    assert usage_acc.to_wire()["worker"]["prompt_tokens"] == 40
    llm_span = next(
        s for s in memory_span_exporter.get_finished_spans() if s.name.startswith("react.llm.turn_")
    )
    assert llm_span.attributes["llm.token_count.prompt"] == 40
    assert llm_span.attributes["llm.token_count.completion"] == 8
    assert llm_span.attributes["llm.token_count.total"] == 48
    assert llm_span.attributes["llm.model_name"] == "mock-model"
