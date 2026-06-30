"""HITL approve/reject enqueue: idempotency and FAILED outbox requeue."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from common.models import OutboxEvent, OutboxStatus, SagaStatus, StepStatus
from common.plugins import register_engine_hooks, reset_registry
from common.topics import TOPIC_ORCHESTRATOR_EVENTS
from engine.hitl_decisions import (
    enqueue_hitl_retry,
    enqueue_human_decision,
    human_decision_idempotency_key,
    human_retry_outbox_idempotency_key,
)
from tests.factories import create_saga_with_steps


@dataclass
class _RecordingEngineHooks:
    calls: list[str] = field(default_factory=list)

    async def on_hitl_decision_queued(self, **kwargs: object) -> None:
        self.calls.append("decision_queued")

    async def on_step_transition(self, **kwargs: object) -> None:
        return None

    async def on_saga_transition(self, **kwargs: object) -> None:
        return None

    async def on_step_scheduled(self, **kwargs: object) -> None:
        return None

    async def on_step_started(self, **kwargs: object) -> None:
        return None

    async def on_hitl_review_requested(self, **kwargs: object) -> None:
        return None

    async def on_ingest_deduplicated(self, **kwargs: object) -> None:
        return None

    async def on_steps_skipped_summary(self, **kwargs: object) -> None:
        return None

    async def on_saga_created(self, **kwargs: object) -> None:
        return None

    async def on_step_created(self, **kwargs: object) -> None:
        return None

    async def on_compensation_scheduled(self, **kwargs: object) -> None:
        return None

    async def on_hitl_approved(self, **kwargs: object) -> None:
        return None

    async def on_hitl_rejected(self, **kwargs: object) -> None:
        return None

    async def on_hitl_retry_queued(self, **kwargs: object) -> None:
        return None

    async def on_hitl_retry_requested(self, **kwargs: object) -> None:
        return None

    async def on_hitl_expired(self, **kwargs: object) -> None:
        return None

    async def on_reaper_zombie_detected(self, **kwargs: object) -> None:
        return None


@pytest.fixture
def recording_hooks():
    reset_registry()
    hooks = _RecordingEngineHooks()
    register_engine_hooks(hooks)
    yield hooks
    reset_registry()


async def _awaiting_human_saga():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.AWAITING_HUMAN
    await step.save()
    saga.status = SagaStatus.AWAITING_HUMAN
    await saga.save()
    return saga, step


@pytest.mark.asyncio
async def test_enqueue_human_decision_queues_fresh_approve(recording_hooks):
    saga, step = await _awaiting_human_saga()

    result = await enqueue_human_decision(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        namespace=saga.namespace,
        decision="APPROVE",
    )

    key = human_decision_idempotency_key(trace_id=saga.trace_id, step_span_id=step.span_id)
    assert result == {"status": "queued", "idempotency_key": key}
    assert "decision_queued" in recording_hooks.calls


@pytest.mark.asyncio
async def test_enqueue_human_decision_already_queued_when_pending(recording_hooks):
    saga, step = await _awaiting_human_saga()
    key = human_decision_idempotency_key(trace_id=saga.trace_id, step_span_id=step.span_id)
    await OutboxEvent.create(
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        event_type="HUMAN_APPROVED",
        destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
        idempotency_key=key,
        status=OutboxStatus.PENDING,
        payload={"event_type": "HUMAN_APPROVED"},
    )

    result = await enqueue_human_decision(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        namespace=saga.namespace,
        decision="APPROVE",
    )

    assert result == {"status": "already_queued", "idempotency_key": key}
    assert recording_hooks.calls == []


@pytest.mark.asyncio
async def test_enqueue_human_decision_requeues_failed_outbox(recording_hooks):
    saga, step = await _awaiting_human_saga()
    key = human_decision_idempotency_key(trace_id=saga.trace_id, step_span_id=step.span_id)
    row = await OutboxEvent.create(
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        event_type="HUMAN_APPROVED",
        destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
        idempotency_key=key,
        status=OutboxStatus.FAILED,
        payload={"event_type": "HUMAN_APPROVED"},
    )

    result = await enqueue_human_decision(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        namespace=saga.namespace,
        decision="APPROVE",
    )

    assert result == {"status": "requeued", "idempotency_key": key}
    await row.refresh_from_db()
    assert row.status == OutboxStatus.PENDING
    assert recording_hooks.calls == []


@pytest.mark.asyncio
async def test_enqueue_hitl_retry_requeues_failed_outbox(recording_hooks):
    saga, step = await _awaiting_human_saga()
    token = "retry-token-abc12345"
    retry_key = human_retry_outbox_idempotency_key(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        retry_token=token,
    )
    row = await OutboxEvent.create(
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        event_type="HUMAN_RETRY",
        destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
        idempotency_key=retry_key,
        status=OutboxStatus.FAILED,
        payload={"event_type": "HUMAN_RETRY"},
    )

    result = await enqueue_hitl_retry(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        namespace=saga.namespace,
        retry_token=token,
    )

    assert result["status"] == "requeued"
    assert result["idempotency_key"] == retry_key
    await row.refresh_from_db()
    assert row.status == OutboxStatus.PENDING
