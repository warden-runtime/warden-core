"""Tests for outbox-level idempotency: duplicate emissions produce a single row."""

import pytest
from common.contracts import DoStepCommand, SagaEventPayload
from common.models import OutboxEvent
from common.outbox import emit_saga_event
from common.topics import TOPIC_ORCHESTRATOR_EVENTS, TOPIC_WORKER_COMMANDS


@pytest.mark.asyncio
async def test_emit_same_worker_command_twice_creates_single_outbox_row():
    """Two emit_saga_event calls with the same idempotency_key (worker command) result in one row."""
    trace_id = "a" * 32
    span_id = "b" * 16
    idem_key = "test-do-step-idem-key"
    command = DoStepCommand(
        type="DO_STEP",
        namespace="default",
        saga_trace_id=trace_id,
        step_span_id=span_id,
        worker_name="test-worker",
        worker_version="1.0.0",
        idempotency_key=idem_key,
        prompt_ref="p.j2",
        arguments={},
        tool_specs=[],
    )

    await emit_saga_event(
        topic=TOPIC_WORKER_COMMANDS,
        event_type="DO_STEP",
        payload_schema=command,
    )
    await emit_saga_event(
        topic=TOPIC_WORKER_COMMANDS,
        event_type="DO_STEP",
        payload_schema=command,
    )

    rows = await OutboxEvent.filter(
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=idem_key,
    ).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_emit_same_orchestrator_event_twice_creates_single_outbox_row():
    """Two emits of the same orchestrator event (same trace_id, event_type, step_span_id) result in one row."""
    trace_id = "c" * 32
    payload = SagaEventPayload(
        namespace="default",
        saga_trace_id=trace_id,
        step_span_id=None,
        status="STARTED",
        output={},
    )

    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type="SAGA_STARTED",
        payload_schema=payload,
    )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type="SAGA_STARTED",
        payload_schema=payload,
    )

    derived_key = f"{trace_id}:SAGA_STARTED:"
    rows = await OutboxEvent.filter(
        destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
        idempotency_key=derived_key,
    ).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_allowed_in_different_namespaces():
    """Dedup is per namespace: identical keys in different tenants do not collide."""
    trace_id = "d" * 32
    span_id = "e" * 16
    idem_key = "shared-batch-idem-key"
    for namespace in ("sandbox", "production"):
        command = DoStepCommand(
            type="DO_STEP",
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=span_id,
            worker_name="test-worker",
            worker_version="1.0.0",
            idempotency_key=idem_key,
            prompt_ref="p.j2",
            arguments={},
            tool_specs=[],
        )
        await emit_saga_event(
            topic=TOPIC_WORKER_COMMANDS,
            event_type="DO_STEP",
            payload_schema=command,
        )

    assert (
        await OutboxEvent.filter(
            namespace="sandbox",
            destination_topic=TOPIC_WORKER_COMMANDS,
            idempotency_key=idem_key,
        ).count()
        == 1
    )
    assert (
        await OutboxEvent.filter(
            namespace="production",
            destination_topic=TOPIC_WORKER_COMMANDS,
            idempotency_key=idem_key,
        ).count()
        == 1
    )
