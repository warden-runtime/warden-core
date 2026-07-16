"""OpenInference-oriented child spans for ReAct LLM and tool turns."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from common.telemetry import get_bound_log_context, safe_truncate_tag
from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import Iterator

    from common.llm import ChatResponse, ToolCall

OPENINFERENCE_SPAN_KIND = "openinference.span.kind"


def _json_preview(value: Any) -> str:
    try:
        raw = json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        raw = str(value)
    return safe_truncate_tag(raw)


def _warden_native_tool_calls(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [
        {"id": tc.id, "name": tc.name, "arguments": tc.args if isinstance(tc.args, dict) else {}}
        for tc in tool_calls
    ]


def _apply_ledger_attrs(span: trace.Span) -> None:
    bound = get_bound_log_context()
    if bound.get("trace_id"):
        span.set_attribute("saga.id", safe_truncate_tag(bound["trace_id"]))
    if bound.get("span_id"):
        span.set_attribute("saga.step_span_id", safe_truncate_tag(bound["span_id"]))
    if bound.get("step_id"):
        span.set_attribute("saga.step_id", safe_truncate_tag(bound["step_id"]))


@contextmanager
def react_llm_span(*, turn_index: int, message_count: int) -> Iterator[trace.Span]:
    tracer = trace.get_tracer("warden.react")
    with tracer.start_as_current_span(f"react.llm.turn_{turn_index}") as span:
        _apply_ledger_attrs(span)
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "LLM")
        span.set_attribute("react.turn_index", turn_index)
        span.set_attribute("react.message_count", message_count)
        yield span


def _set_usage_attrs(span: trace.Span, usage: Any) -> None:
    if usage is None:
        return
    mapping = (
        ("llm.token_count.prompt", usage.prompt_tokens),
        ("llm.token_count.completion", usage.completion_tokens),
        ("llm.token_count.total", usage.total_tokens),
    )
    for attr, val in mapping:
        if val > 0:
            span.set_attribute(attr, val)
    if usage.model_id:
        span.set_attribute("llm.model_name", safe_truncate_tag(usage.model_id))
    for key, val in (usage.details or {}).items():
        if val > 0:
            span.set_attribute(f"llm.token_count.{key}", val)


def mark_llm_response(span: trace.Span, response: ChatResponse) -> None:
    if response.content:
        span.set_attribute("output.value", safe_truncate_tag(response.content))
    if response.tool_calls:
        span.set_attribute(
            "llm.tool_calls",
            _json_preview(_warden_native_tool_calls(response.tool_calls)),
        )
    _set_usage_attrs(span, response.usage)


@contextmanager
def react_tool_span(*, tool_call: ToolCall, turn_index: int) -> Iterator[trace.Span]:
    tracer = trace.get_tracer("warden.react")
    with tracer.start_as_current_span(f"react.tool.{tool_call.name}") as span:
        _apply_ledger_attrs(span)
        span.set_attribute(OPENINFERENCE_SPAN_KIND, "TOOL")
        span.set_attribute("tool.name", tool_call.name)
        span.set_attribute("react.turn_index", turn_index)
        if tool_call.id:
            span.set_attribute("tool.call.id", tool_call.id)
        span.set_attribute(
            "tool.parameters",
            _json_preview(tool_call.args if isinstance(tool_call.args, dict) else {}),
        )
        yield span


def mark_tool_output(span: trace.Span, output: str) -> None:
    span.set_attribute("output.value", safe_truncate_tag(output))
