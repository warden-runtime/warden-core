"""HITL manual retry: outbox enqueue, FSM handler, worker claim release."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from common.models import (
    EventType,
    OutboxEvent,
    ProcessedCommand,
    ProcessedIngestEvent,
    SagaStatus,
    StepStatus,
)
from common.plugins import register_engine_hooks, reset_registry
from common.topics import TOPIC_ORCHESTRATOR_EVENTS, TOPIC_WORKER_COMMANDS
from engine.hitl_decisions import enqueue_hitl_retry, human_retry_outbox_idempotency_key
from engine.logic import process_saga_event
from tests.factories import create_saga_with_steps


@dataclass
class _RecordingEngineHooks:
    calls: list[str] = field(default_factory=list)
    kwargs: list[dict] = field(default_factory=list)

    async def on_hitl_retry_queued(self, **kwargs: object) -> None:
        self.calls.append("retry_queued")
        self.kwargs.append(dict(kwargs))

    async def on_hitl_retry_requested(self, **kwargs: object) -> None:
        self.calls.append("retry_requested")
        self.kwargs.append(dict(kwargs))

    async def on_step_transition(self, **kwargs: object) -> None:
        self.calls.append("step_transition")

    async def on_saga_transition(self, **kwargs: object) -> None:
        self.calls.append("saga_transition")

    async def on_step_scheduled(self, **kwargs: object) -> None:
        self.calls.append("step_scheduled")

    async def on_step_started(self, **kwargs: object) -> None:
        self.calls.append("step_started")

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

    async def on_hitl_decision_queued(self, **kwargs: object) -> None:
        return None

    async def on_hitl_expired(self, **kwargs: object) -> None:
        return None

    async def on_reaper_zombie_detected(self, **kwargs: object) -> None:
        return None

    async def on_reaper_timeout_enforced(self, **kwargs: object) -> None:
        return None

    async def on_reaper_race_skipped(self, **kwargs: object) -> None:
        return None

    async def on_manifest_registered(self, **kwargs: object) -> None:
        return None


@pytest.fixture
def recording_hooks():
    reset_registry()
    hooks = _RecordingEngineHooks()
    register_engine_hooks(hooks)
    yield hooks
    reset_registry()


@pytest.mark.asyncio
async def test_enqueue_hitl_retry_writes_human_retry_outbox(recording_hooks):
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.AWAITING_HUMAN
    step.pending_review_payload = {"data": {"x": 1}}
    await step.save()
    saga.status = SagaStatus.AWAITING_HUMAN
    await saga.save()

    token = uuid.uuid4().hex
    result = await enqueue_hitl_retry(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        namespace=saga.namespace,
        retry_token=token,
    )
    assert result["status"] == "queued"
    retry_key = human_retry_outbox_idempotency_key(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        retry_token=token,
    )
    assert result["idempotency_key"] == retry_key
    assert await OutboxEvent.filter(
        destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
        idempotency_key=retry_key,
    ).exists()
    assert "retry_queued" in recording_hooks.calls


@pytest.mark.asyncio
async def test_enqueue_hitl_retry_idempotent_per_token(recording_hooks):
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.AWAITING_HUMAN
    await step.save()
    saga.status = SagaStatus.AWAITING_HUMAN
    await saga.save()

    token = "fixed-retry-token-abc12345"
    first = await enqueue_hitl_retry(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        namespace=saga.namespace,
        retry_token=token,
    )
    second = await enqueue_hitl_retry(
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        namespace=saga.namespace,
        retry_token=token,
    )
    assert first["status"] == "queued"
    assert second["status"] == "already_queued"
    assert await OutboxEvent.filter(destination_topic=TOPIC_ORCHESTRATOR_EVENTS).count() == 1


@pytest.mark.asyncio
async def test_human_retry_requeues_worker_and_preserves_context(recording_hooks):
    saga, steps = await create_saga_with_steps(
        step_count=2,
        initial_context={
            "input": {"n": 1},
            "steps": {"step_0": {"output": {"data": {"ok": True}}}},
        },
    )
    step0, step1 = steps[0], steps[1]
    prior_key = f"{saga.trace_id}-step_0"
    step0.idempotency_key = prior_key
    step0.status = StepStatus.AWAITING_HUMAN
    step0.pending_review_payload = {"data": {"draft": True}}
    await step0.save()
    step1.status = StepStatus.PENDING
    await step1.save()
    saga.status = SagaStatus.AWAITING_HUMAN
    await saga.save()
    await ProcessedCommand.create(
        idempotency_key=prior_key,
        namespace=saga.namespace,
        result_emitted=True,
    )
    dedup_key = f"{saga.trace_id}:{EventType.STEP_COMPLETED.value}:{step0.span_id}"
    await ProcessedIngestEvent.create(event_dedup_key=dedup_key)
    context_before = dict(saga.context or {})

    with patch("engine.logic.assert_prompt_file_exists"):
        await process_saga_event(
            {
                "event_type": EventType.HUMAN_RETRY.value,
                "saga_trace_id": saga.trace_id,
                "step_span_id": step0.span_id,
                "namespace": saga.namespace,
            }
        )

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    assert saga.status == SagaStatus.RUNNING
    assert step0.status == StepStatus.IN_PROGRESS
    assert step0.pending_review_payload is None
    assert step0.idempotency_key != prior_key
    assert saga.context == context_before
    assert not await ProcessedCommand.filter(idempotency_key=prior_key).exists()
    dedup_key = f"{saga.trace_id}:{EventType.STEP_COMPLETED.value}:{step0.span_id}"
    assert not await ProcessedIngestEvent.filter(event_dedup_key=dedup_key).exists()
    worker_events = await OutboxEvent.filter(
        destination_topic=TOPIC_WORKER_COMMANDS,
        saga_trace_id=saga.trace_id,
    ).all()
    assert len(worker_events) == 1
    assert worker_events[0].payload.get("type") == "DO_STEP"
    assert worker_events[0].payload.get("idempotency_key") == step0.idempotency_key
    assert step0.hitl_retry_count == 1
    assert worker_events[0].payload.get("arguments", {}).get("_hitl_retry", {}).get("attempt") == 1
    assert "retry_requested" in recording_hooks.calls
    assert "saga_transition" in recording_hooks.calls
    assert "step_transition" in recording_hooks.calls


@pytest.mark.asyncio
async def test_human_retry_merges_guidance_into_worker_command(recording_hooks):
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.AWAITING_HUMAN
    step.hitl_retry_guidance = "manifest hint"
    await step.save()
    saga.status = SagaStatus.AWAITING_HUMAN
    await saga.save()

    with patch("engine.logic.assert_prompt_file_exists"):
        await process_saga_event(
            {
                "event_type": EventType.HUMAN_RETRY.value,
                "saga_trace_id": saga.trace_id,
                "step_span_id": step.span_id,
                "namespace": saga.namespace,
                "retry_guidance": "override for this run",
            }
        )

    worker_events = await OutboxEvent.filter(
        destination_topic=TOPIC_WORKER_COMMANDS,
        saga_trace_id=saga.trace_id,
    ).all()
    hitl_ctx = worker_events[0].payload.get("arguments", {}).get("_hitl_retry", {})
    assert hitl_ctx.get("guidance") == "override for this run"
    assert hitl_ctx.get("attempt") == 1
