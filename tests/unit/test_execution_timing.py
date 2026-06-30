"""Unit tests for execution timing helpers and accumulators."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from common.execution_timing import (
    DISPATCH_PERF_ANCHOR_KEY,
    EngineTimingAccumulator,
    WorkerTimingAccumulator,
    compute_dispatch_to_ingest_ms,
    merge_execution_timing,
    merge_pending_engine,
    pending_engine_payload,
    worker_timing_from_event,
)
from common.models import SagaStepInstance
from engine.execution_timing import (
    add_engine_bucket_ms,
    finalize_step_execution_timing,
    merge_step_timing_if_needed,
)
from opentelemetry import trace
from tests.factories import create_saga_with_steps
from tortoise.transactions import in_transaction


def test_worker_timing_accumulator_buckets():
    acc = WorkerTimingAccumulator()
    acc.add_ms("hydration_ms", 5)
    acc.add_ms("setup_ms", 10)
    acc.add_ms("llm_ms", 100)
    acc.add_ms("tool_ms", 40)
    assert acc.to_dict() == {
        "hydration_ms": 5,
        "setup_ms": 10,
        "llm_ms": 100,
        "tool_ms": 40,
    }
    assert acc.to_wire() == {"worker": acc.to_dict()}


def test_engine_timing_accumulator_pending_shape():
    acc = EngineTimingAccumulator()
    acc.add_ms("schedule_ms", 22)
    acc.set_dispatch_anchor(1.0)
    pending = acc.to_pending()
    assert pending["engine"]["schedule_ms"] == 22
    assert pending[DISPATCH_PERF_ANCHOR_KEY] == 1.0


def test_merge_execution_timing_worker_and_engine():
    merged = merge_execution_timing(
        worker={"hydration_ms": 8, "setup_ms": 12},
        engine={"schedule_ms": 30, "dispatch_to_ingest_ms": 500},
    )
    assert merged == {
        "worker": {"hydration_ms": 8, "setup_ms": 12},
        "engine": {"schedule_ms": 30, "dispatch_to_ingest_ms": 500},
    }


def test_worker_timing_from_event_wire_shape():
    wire = {"worker": {"tool_ms": 45, "llm_ms": 0}}
    assert worker_timing_from_event(wire) == {"tool_ms": 45}


def test_compute_dispatch_to_ingest_ms_outbox_fallback():
    created = datetime.now(UTC) - timedelta(milliseconds=250)
    now = datetime.now(UTC)
    ms = compute_dispatch_to_ingest_ms(anchor=None, outbox_created_at=created, now=now)
    assert ms is not None
    assert ms >= 200


def test_compute_dispatch_to_ingest_ms_rejects_stale_anchor():
    stale_anchor = time.perf_counter() - (25 * 3600)
    ms = compute_dispatch_to_ingest_ms(anchor=stale_anchor, outbox_created_at=None)
    assert ms is None


def test_merge_pending_engine_accumulates_buckets():
    base = pending_engine_payload({"schedule_ms": 10}, dispatch_anchor=2.5)
    merged = merge_pending_engine(base, engine_add={"policy_ms": 5})
    assert merged["engine"]["schedule_ms"] == 10
    assert merged["engine"]["policy_ms"] == 5
    assert merged[DISPATCH_PERF_ANCHOR_KEY] == 2.5


@pytest.mark.asyncio
async def test_finalize_step_execution_timing_merges_and_clears_pending(memory_span_exporter):
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    anchor = time.perf_counter()
    step.pending_engine_timing = pending_engine_payload({"schedule_ms": 15}, dispatch_anchor=anchor)
    await step.save(update_fields=["pending_engine_timing"])

    tracer = trace.get_tracer("test")
    async with in_transaction() as conn:
        with tracer.start_as_current_span("handle_step_completed"):
            merged = await finalize_step_execution_timing(
                step,
                worker_timing={"worker": {"hydration_ms": 3, "setup_ms": 7}},
                conn=conn,
            )
    assert merged["worker"]["hydration_ms"] == 3
    assert merged["engine"]["schedule_ms"] == 15
    assert merged["engine"].get("dispatch_to_ingest_ms", 0) >= 0
    assert step.pending_engine_timing is None
    spans = memory_span_exporter.get_finished_spans()
    assert len(spans) == 1
    ingest_span = spans[0]
    assert ingest_span.name == "handle_step_completed"
    assert ingest_span.attributes.get("timing.engine.dispatch_to_ingest_ms", 0) >= 0


@pytest.mark.asyncio
async def test_finalize_step_execution_timing_merges_and_clears_pending_no_span():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    anchor = time.perf_counter()
    step.pending_engine_timing = pending_engine_payload({"schedule_ms": 15}, dispatch_anchor=anchor)
    await step.save(update_fields=["pending_engine_timing"])

    async with in_transaction() as conn:
        merged = await finalize_step_execution_timing(
            step,
            worker_timing={"worker": {"hydration_ms": 3, "setup_ms": 7}},
            conn=conn,
        )
    assert merged["worker"]["hydration_ms"] == 3
    assert merged["engine"]["schedule_ms"] == 15
    assert merged["engine"].get("dispatch_to_ingest_ms", 0) >= 0
    assert step.pending_engine_timing is None


@pytest.mark.asyncio
async def test_merge_step_timing_if_needed_skips_when_already_finalized():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.execution_timing = {"worker": {"tool_ms": 1}, "engine": {"schedule_ms": 2}}
    step.pending_engine_timing = None
    await merge_step_timing_if_needed(step, worker_timing={"worker": {"tool_ms": 99}}, conn=None)
    assert step.execution_timing["worker"]["tool_ms"] == 1


def test_add_engine_bucket_ms_appends(memory_span_exporter):
    step = SagaStepInstance()
    step.execution_timing = {"engine": {"policy_ms": 10}}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("handle_step_completed"):
        add_engine_bucket_ms(step, bucket="policy_ms", ms=5)
    assert step.execution_timing["engine"]["policy_ms"] == 15
    spans = memory_span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["timing.engine.policy_ms"] == 15
