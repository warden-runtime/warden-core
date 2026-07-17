"""Unit tests for worker LLM usage accumulation and engine finalize."""

from __future__ import annotations

import pytest
from common.execution_usage import (
    WorkerUsageAccumulator,
    merge_execution_usage,
    worker_usage_from_event,
)
from common.llm import TokenUsage
from common.models import SagaStepInstance
from common.telemetry import log_context
from engine.execution_timing import clear_step_timing_fields
from engine.execution_usage import finalize_step_execution_usage
from opentelemetry import trace
from workers.llm.message_content import aimessage_to_chat_response, token_usage_from_aimessage


def test_token_usage_from_aimessage_openai_shape():
    from langchain_core.messages import AIMessage

    msg = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
            "input_token_details": {"cache_read": 8},
            "output_token_details": {"reasoning": 2},
        },
        response_metadata={"model_name": "gpt-4o-mini"},
    )
    usage = token_usage_from_aimessage(msg)
    assert usage is not None
    assert usage.prompt_tokens == 12
    assert usage.completion_tokens == 4
    assert usage.total_tokens == 16
    assert usage.model_id == "gpt-4o-mini"
    assert usage.details.get("cache_read_tokens") == 8
    assert usage.details.get("reasoning_tokens") == 2
    resp = aimessage_to_chat_response(msg)
    assert resp.usage == usage


def test_token_usage_from_aimessage_anthropic_shape():
    from langchain_core.messages import AIMessage

    msg = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {
                "cache_read": 40,
                "cache_creation": 10,
            },
        },
        response_metadata={"model": "claude-sonnet-4-20250514"},
    )
    usage = token_usage_from_aimessage(msg)
    assert usage is not None
    assert usage.model_id == "claude-sonnet-4-20250514"
    assert usage.details.get("cache_read_tokens") == 40
    assert usage.details.get("cache_creation_tokens") == 10


def test_token_usage_absent_returns_none():
    from langchain_core.messages import AIMessage

    assert token_usage_from_aimessage(AIMessage(content="no usage")) is None


def test_worker_usage_accumulator_sums_turns(memory_span_exporter):
    acc = WorkerUsageAccumulator()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("handle_worker_command"):
        with log_context(trace_id="t", span_id="s", step_id="step"):
            acc.add(
                TokenUsage(
                    prompt_tokens=10,
                    completion_tokens=2,
                    total_tokens=12,
                    model_id="m1",
                    details={"cache_read_tokens": 3},
                )
            )
            acc.add(
                TokenUsage(
                    prompt_tokens=5,
                    completion_tokens=1,
                    total_tokens=6,
                    model_id="m2",
                    details={"cache_read_tokens": 1, "reasoning_tokens": 4},
                )
            )
    wire = acc.to_wire()
    assert wire == {
        "worker": {
            "prompt_tokens": 15,
            "completion_tokens": 3,
            "total_tokens": 18,
            "llm_calls": 2,
            "model_id": "m2",
            "details": {"cache_read_tokens": 4, "reasoning_tokens": 4},
        }
    }
    spans = memory_span_exporter.get_finished_spans()
    assert spans[-1].attributes["usage.worker.prompt_tokens"] == 15
    assert spans[-1].attributes["usage.worker.llm_calls"] == 2


def test_worker_usage_accumulator_memory_stats():
    from workers.adapters.react_memory import CompressionStats

    acc = WorkerUsageAccumulator()
    acc.add_memory_stats(
        CompressionStats(
            compressed=True,
            deepest_tier=2,
            groups_evicted=3,
            estimated_tokens_saved=100,
            tier1_redactions=1,
        )
    )
    acc.add_memory_stats(
        CompressionStats(
            compressed=True,
            deepest_tier=3,
            groups_evicted=1,
            estimated_tokens_saved=50,
        )
    )
    wire = acc.to_wire()
    assert wire["worker"]["memory"] == {
        "compressions": 2,
        "groups_evicted": 4,
        "estimated_tokens_saved": 150,
        "max_tier": 3,
        "tier1_redactions": 1,
    }
    merged = merge_execution_usage(worker=wire["worker"])
    assert merged["worker"]["memory"]["max_tier"] == 3


def test_worker_usage_from_event_and_merge():
    assert worker_usage_from_event(None) == {}
    assert worker_usage_from_event({"prompt_tokens": 3, "total_tokens": 3})["prompt_tokens"] == 3
    merged = merge_execution_usage(
        worker={"prompt_tokens": 9, "total_tokens": 9, "model_id": "x"},
        existing={"worker": {"prompt_tokens": 1, "llm_calls": 1}},
    )
    assert merged["worker"]["prompt_tokens"] == 9
    assert merged["worker"]["llm_calls"] == 1
    assert merged["worker"]["model_id"] == "x"


@pytest.mark.asyncio
async def test_finalize_step_execution_usage():
    step = SagaStepInstance()
    step.execution_usage = None
    merged = await finalize_step_execution_usage(
        step,
        worker_usage={
            "worker": {
                "prompt_tokens": 11,
                "completion_tokens": 2,
                "total_tokens": 13,
                "llm_calls": 1,
                "model_id": "gpt-test",
            }
        },
        conn=None,
    )
    assert merged == step.execution_usage
    assert step.execution_usage["worker"]["prompt_tokens"] == 11
    assert step.execution_usage["worker"]["model_id"] == "gpt-test"


def test_clear_step_timing_fields_also_clears_usage():
    step = SagaStepInstance()
    step.execution_timing = {"worker": {"llm_ms": 1}}
    step.pending_engine_timing = {"engine": {"schedule_ms": 1}}
    step.execution_usage = {"worker": {"prompt_tokens": 1}}
    clear_step_timing_fields(step)
    assert step.execution_timing is None
    assert step.pending_engine_timing is None
    assert step.execution_usage is None


def test_effective_max_step_tokens_prefers_step_value(monkeypatch):
    from common.execution_usage import effective_max_step_tokens

    monkeypatch.setenv("WARDEN_MAX_STEP_TOKENS", "999")
    assert effective_max_step_tokens(50) == 50
    assert effective_max_step_tokens(None) == 999


def test_effective_max_step_tokens_env_zero_means_unlimited(monkeypatch):
    from common.execution_usage import effective_max_step_tokens

    monkeypatch.setenv("WARDEN_MAX_STEP_TOKENS", "0")
    assert effective_max_step_tokens(None) is None
    monkeypatch.delenv("WARDEN_MAX_STEP_TOKENS", raising=False)
    assert effective_max_step_tokens(None) is None


def test_enforce_step_token_budget_raises_with_forensics():
    from common.agent_adapter import ExecutionStepError
    from common.execution_usage import WorkerUsageAccumulator, enforce_step_token_budget
    from common.llm import TokenUsage

    acc = WorkerUsageAccumulator()
    acc.add(TokenUsage(prompt_tokens=70, completion_tokens=30, total_tokens=100))
    with pytest.raises(ExecutionStepError) as exc_info:
        enforce_step_token_budget(acc, 50)
    details = exc_info.value.error_details or {}
    assert details["code"] == "STEP_TOKEN_LIMIT_EXCEEDED"
    assert details["tokens_used"] == 100
    assert details["max_step_tokens"] == 50
    assert details["prompt_tokens"] == 70
    assert details["completion_tokens"] == 30


def test_enforce_step_token_budget_noop_when_unlimited_or_under():
    from common.execution_usage import WorkerUsageAccumulator, enforce_step_token_budget
    from common.llm import TokenUsage

    acc = WorkerUsageAccumulator()
    acc.add(TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    enforce_step_token_budget(acc, None)
    enforce_step_token_budget(acc, 15)
    enforce_step_token_budget(acc, 100)
