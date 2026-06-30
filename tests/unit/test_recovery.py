"""Unit tests for operator saga recovery."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from common.models import (
    OutboxEvent,
    OutboxStatus,
    ProcessedCommand,
    ProcessedOperatorRecovery,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
)
from common.plugins import register_engine_hooks, reset_registry
from common.topics import TOPIC_WORKER_COMMANDS
from engine.recovery import enqueue_compensation_retry, enqueue_step_retry
from engine.recovery_errors import RecoveryConflictError
from tests.factories import create_saga_with_steps


@dataclass
class _RecordingEngineHooks:
    calls: list[str] = field(default_factory=list)
    raise_on: str | None = None

    async def on_operator_recovery_requested(self, **kwargs: object) -> None:
        if self.raise_on == "operator_recovery_requested":
            raise RuntimeError("hook failed")
        self.calls.append("operator_recovery_requested")


@pytest.fixture
def recording_hooks():
    reset_registry()
    hooks = _RecordingEngineHooks()
    register_engine_hooks(hooks)
    yield hooks
    reset_registry()


def _recovery_token() -> str:
    return uuid.uuid4().hex


async def _running_in_progress_step(*, step_kind: str = "reason"):
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.step_kind = step_kind
    step.status = StepStatus.IN_PROGRESS
    await step.save()
    saga.status = SagaStatus.RUNNING
    await saga.save()
    return saga, step


async def _failed_worker_outbox(*, saga, step):
    await OutboxEvent.create(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        event_type="DO_STEP",
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=step.idempotency_key,
        payload={"type": "DO_STEP", "idempotency_key": step.idempotency_key},
        status=OutboxStatus.FAILED,
    )


@pytest.mark.asyncio
async def test_retry_step_force_commit_without_destructive_raises():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.step_kind = "commit"
    step.status = StepStatus.IN_PROGRESS
    await step.save()
    saga.status = SagaStatus.RUNNING
    await saga.save()

    with pytest.raises(RecoveryConflictError, match="allow_destructive"):
        await enqueue_step_retry(
            namespace=saga.namespace,
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
            force=True,
            allow_destructive=False,
        )


@pytest.mark.asyncio
async def test_retry_step_requeues_failed_outbox(recording_hooks):
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.IN_PROGRESS
    await step.save()
    saga.status = SagaStatus.RUNNING
    await saga.save()
    await OutboxEvent.create(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        event_type="DO_STEP",
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=step.idempotency_key,
        payload={"type": "DO_STEP", "idempotency_key": step.idempotency_key},
        status=OutboxStatus.FAILED,
    )

    result = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )
    assert result["status"] == "requeued"
    row = await OutboxEvent.filter(idempotency_key=step.idempotency_key).first()
    assert row is not None
    assert row.status == OutboxStatus.PENDING
    assert "operator_recovery_requested" in recording_hooks.calls


@pytest.mark.asyncio
async def test_retry_step_claim_active_without_force():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.IN_PROGRESS
    await step.save()
    saga.status = SagaStatus.RUNNING
    await saga.save()
    await ProcessedCommand.create(
        idempotency_key=step.idempotency_key,
        namespace=saga.namespace,
        result_emitted=False,
    )

    result = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )
    assert result["status"] == "claim_active"


@pytest.mark.asyncio
async def test_retry_step_force_reason_releases_active_claim(recording_hooks):
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.step_kind = "reason"
    step.status = StepStatus.IN_PROGRESS
    await step.save()
    saga.status = SagaStatus.RUNNING
    await saga.save()
    await ProcessedCommand.create(
        idempotency_key=step.idempotency_key,
        namespace=saga.namespace,
        result_emitted=False,
    )
    await OutboxEvent.create(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        event_type="DO_STEP",
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=step.idempotency_key,
        payload={"type": "DO_STEP", "idempotency_key": step.idempotency_key},
        status=OutboxStatus.IN_PROGRESS,
    )

    result = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        force=True,
    )
    assert result["status"] == "requeued"
    assert await ProcessedCommand.filter(idempotency_key=step.idempotency_key).count() == 0
    row = await OutboxEvent.filter(idempotency_key=step.idempotency_key).first()
    assert row is not None
    assert row.status == OutboxStatus.PENDING
    assert "operator_recovery_requested" in recording_hooks.calls


@pytest.mark.asyncio
async def test_retry_step_stale_in_progress_outbox_requeues_without_force(recording_hooks):
    from datetime import timedelta

    from common.outbox_reap import stale_outbox_cutoff

    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.IN_PROGRESS
    await step.save()
    saga.status = SagaStatus.RUNNING
    await saga.save()
    row = await OutboxEvent.create(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        event_type="DO_STEP",
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=step.idempotency_key,
        payload={"type": "DO_STEP", "idempotency_key": step.idempotency_key},
        status=OutboxStatus.IN_PROGRESS,
    )
    await OutboxEvent.filter(id=row.id).update(
        updated_at=stale_outbox_cutoff() - timedelta(seconds=30),
    )

    result = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )
    assert result["status"] == "requeued"
    await row.refresh_from_db()
    assert row.status == OutboxStatus.PENDING


@pytest.mark.asyncio
async def test_retry_step_hook_failure_rolls_back_transaction():
    reset_registry()
    hooks = _RecordingEngineHooks(raise_on="operator_recovery_requested")
    register_engine_hooks(hooks)
    try:
        saga, steps = await create_saga_with_steps(step_count=1)
        step = steps[0]
        step.status = StepStatus.IN_PROGRESS
        await step.save()
        saga.status = SagaStatus.RUNNING
        await saga.save()
        await OutboxEvent.create(
            namespace=saga.namespace,
            saga_trace_id=saga.trace_id,
            step_span_id=step.span_id,
            event_type="DO_STEP",
            destination_topic=TOPIC_WORKER_COMMANDS,
            idempotency_key=step.idempotency_key,
            payload={"type": "DO_STEP", "idempotency_key": step.idempotency_key},
            status=OutboxStatus.FAILED,
        )

        with pytest.raises(RuntimeError, match="hook failed"):
            await enqueue_step_retry(
                namespace=saga.namespace,
                trace_id=saga.trace_id,
                step_span_id=step.span_id,
            )

        row = await OutboxEvent.filter(idempotency_key=step.idempotency_key).first()
        assert row is not None
        assert row.status == OutboxStatus.FAILED
    finally:
        reset_registry()


@pytest.mark.asyncio
async def test_retry_step_same_recovery_token_dedupes(recording_hooks):
    saga, step = await _running_in_progress_step()
    await _failed_worker_outbox(saga=saga, step=step)
    token = _recovery_token()

    first = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        recovery_token=token,
    )
    second = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        recovery_token=token,
    )

    assert first == second
    assert first["status"] == "requeued"
    assert recording_hooks.calls.count("operator_recovery_requested") == 1
    assert await ProcessedOperatorRecovery.filter().count() == 1


@pytest.mark.asyncio
async def test_retry_step_recovery_token_fingerprint_conflict():
    saga, step = await _running_in_progress_step()
    await _failed_worker_outbox(saga=saga, step=step)
    token = _recovery_token()

    await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        recovery_token=token,
        force=False,
    )
    with pytest.raises(RecoveryConflictError, match="different request parameters"):
        await enqueue_step_retry(
            namespace=saga.namespace,
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
            recovery_token=token,
            force=True,
        )


@pytest.mark.asyncio
async def test_retry_step_without_recovery_token_allows_duplicate_side_effects(recording_hooks):
    saga, step = await _running_in_progress_step()
    await _failed_worker_outbox(saga=saga, step=step)

    first = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )
    second = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )

    assert first["status"] == "requeued"
    assert second["status"] == "requeued"
    assert first["recovery_token"] != second["recovery_token"]
    assert recording_hooks.calls.count("operator_recovery_requested") == 2
    assert await ProcessedOperatorRecovery.filter().count() == 0


@pytest.mark.asyncio
async def test_retry_step_claim_active_recovery_token_replays(recording_hooks):
    saga, step = await _running_in_progress_step()
    await ProcessedCommand.create(
        idempotency_key=step.idempotency_key,
        namespace=saga.namespace,
        result_emitted=False,
    )
    token = _recovery_token()

    first = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        recovery_token=token,
    )
    second = await enqueue_step_retry(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        recovery_token=token,
    )

    assert first["status"] == "claim_active"
    assert first == second
    assert recording_hooks.calls == []


@pytest.mark.asyncio
async def test_retry_step_recovery_token_hook_failure_leaves_no_dedup_row():
    reset_registry()
    hooks = _RecordingEngineHooks(raise_on="operator_recovery_requested")
    register_engine_hooks(hooks)
    try:
        saga, step = await _running_in_progress_step()
        await _failed_worker_outbox(saga=saga, step=step)
        token = _recovery_token()

        with pytest.raises(RuntimeError, match="hook failed"):
            await enqueue_step_retry(
                namespace=saga.namespace,
                trace_id=saga.trace_id,
                step_span_id=step.span_id,
                recovery_token=token,
            )
        assert await ProcessedOperatorRecovery.filter().count() == 0

        hooks.raise_on = None
        result = await enqueue_step_retry(
            namespace=saga.namespace,
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
            recovery_token=token,
        )
        assert result["status"] == "requeued"
        assert await ProcessedOperatorRecovery.filter().count() == 1
    finally:
        reset_registry()


@pytest.mark.asyncio
async def test_retry_compensation_same_recovery_token_dedupes(recording_hooks):
    trace_id = uuid.uuid4().hex
    forward_span = uuid.uuid4().hex[:16]
    comp_span = uuid.uuid4().hex[:16]
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="test-def",
        status=SagaStatus.COMPENSATING,
        context={"input": {}, "steps": {}},
    )
    forward = await SagaStepInstance.create(
        span_id=forward_span,
        saga_trace_id=trace_id,
        namespace="default",
        saga=saga,
        step_id="fwd",
        step_name="fwd",
        order_index=0,
        idempotency_key=f"{trace_id}-fwd",
        status=StepStatus.FAILED,
        worker="test-worker",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=300,
        error_details={"error": "timeout"},
    )
    comp = await SagaStepInstance.create(
        span_id=comp_span,
        compensates_span_id=forward_span,
        saga_trace_id=trace_id,
        namespace="default",
        saga=saga,
        step_id="fwd",
        step_name="fwd",
        order_index=0,
        idempotency_key=f"comp-{trace_id}",
        status=StepStatus.FAILED,
        worker="test-worker",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=300,
    )
    await OutboxEvent.create(
        namespace=saga.namespace,
        saga_trace_id=trace_id,
        step_span_id=comp_span,
        event_type="EXECUTE_COMPENSATION",
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=comp.idempotency_key,
        payload={"type": "EXECUTE_COMPENSATION", "idempotency_key": comp.idempotency_key},
        status=OutboxStatus.FAILED,
    )
    token = _recovery_token()

    first = await enqueue_compensation_retry(
        namespace=saga.namespace,
        trace_id=trace_id,
        step_span_id=comp_span,
        recovery_token=token,
    )
    second = await enqueue_compensation_retry(
        namespace=saga.namespace,
        trace_id=trace_id,
        step_span_id=comp_span,
        recovery_token=token,
    )

    assert first == second
    assert first["status"] == "requeued"
    assert recording_hooks.calls.count("operator_recovery_requested") == 1
    assert forward.span_id == forward_span
