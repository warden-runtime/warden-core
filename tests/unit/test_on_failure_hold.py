"""Unit tests for post-failure hold (on_failure / AWAITING_RECOVERY)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from common.compensation_context import forward_tool_idempotency_key
from common.contracts import StepFailedResultEvent
from common.models import (
    EventType,
    OutboxEvent,
    OutboxStatus,
    ProcessedIngestEvent,
    SagaStatus,
    StepStatus,
)
from common.outbox import emit_saga_event
from common.schemas.failure import OnFailureSpec
from common.schemas.step import parse_step_blueprint
from common.topics import TOPIC_ORCHESTRATOR_EVENTS
from engine.api.read_queries import in_flight_statuses
from engine.logic import _clear_worker_result_ingest_dedup, process_saga_event
from engine.recovery import enqueue_retry_forward, enqueue_start_compensation
from engine.recovery_errors import RecoveryConflictError
from engine.step_materialize import _step_create_fields
from tests.factories import create_saga_with_steps, step_definition_body
from tortoise.transactions import in_transaction


def test_step_blueprint_accepts_on_failure_await_operator() -> None:
    step = parse_step_blueprint(
        step_definition_body(
            on_failure={"strategy": "await_operator"},
        )
    )
    assert step.on_failure is not None
    assert step.on_failure.strategy == "await_operator"
    capability = step.to_capability_dict()
    assert capability["on_failure"] == {"strategy": "await_operator"}


def test_step_blueprint_emits_default_on_failure_strategy() -> None:
    step = parse_step_blueprint(step_definition_body())
    assert step.on_failure is None
    assert step.to_capability_dict()["on_failure"] == {"strategy": "auto_compensate"}


def test_materialize_sets_on_failure_strategy_from_spec() -> None:
    saga = SimpleNamespace(trace_id="trace1", namespace="default")
    fields = _step_create_fields(
        saga=saga,  # type: ignore[arg-type]
        step_spec={
            "id": "run",
            "name": "Run",
            "kind": "reason",
            "worker": "w",
            "worker_version": "1.0.0",
            "prompt": "p.j2",
            "on_failure": {"strategy": "await_operator"},
        },
        forward_seq=0,
        order_index=0,
    )
    assert fields["on_failure_strategy"] == "await_operator"


def test_materialize_defaults_on_failure_strategy() -> None:
    saga = SimpleNamespace(trace_id="trace1", namespace="default")
    fields = _step_create_fields(
        saga=saga,  # type: ignore[arg-type]
        step_spec={
            "id": "run",
            "name": "Run",
            "kind": "reason",
            "worker": "w",
            "worker_version": "1.0.0",
            "prompt": "p.j2",
        },
        forward_seq=0,
        order_index=0,
    )
    assert fields["on_failure_strategy"] == "auto_compensate"


def test_materialize_engine_native_uses_auto_compensate() -> None:
    saga = SimpleNamespace(trace_id="trace1", namespace="default")
    fields = _step_create_fields(
        saga=saga,  # type: ignore[arg-type]
        step_spec={
            "id": "spawn",
            "name": "Spawn",
            "kind": "spawn_sagas",
            "spawn": {
                "saga_name": "child",
                "saga_version": "1.0.0",
                "items_from": "$.input.items",
                "result_from": "$.item.id",
            },
        },
        forward_seq=0,
        order_index=0,
    )
    assert fields["on_failure_strategy"] == "auto_compensate"


def test_in_flight_statuses_include_awaiting_recovery() -> None:
    assert SagaStatus.AWAITING_RECOVERY in in_flight_statuses()


def test_on_failure_spec_defaults_strategy() -> None:
    assert OnFailureSpec().strategy == "auto_compensate"


@pytest.mark.asyncio
async def test_retry_forward_conflict_when_not_on_hold():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.FAILED
    await step.save()
    saga.status = SagaStatus.RUNNING
    await saga.save()

    with pytest.raises(RecoveryConflictError, match="AWAITING_RECOVERY"):
        await enqueue_retry_forward(
            namespace=saga.namespace,
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
        )


@pytest.mark.asyncio
async def test_start_compensation_conflict_when_not_on_hold():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.FAILED
    await step.save()
    saga.status = SagaStatus.FAILED
    await saga.save()

    with pytest.raises(RecoveryConflictError, match="AWAITING_RECOVERY"):
        await enqueue_start_compensation(
            namespace=saga.namespace,
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
        )


@pytest.mark.asyncio
async def test_retry_forward_dirty_without_allow_destructive_raises():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.TIMED_OUT
    step.error_details = {"code": "TIMEOUT", "message": "timed out"}
    await step.save()
    saga.status = SagaStatus.AWAITING_RECOVERY
    await saga.save()

    with pytest.raises(RecoveryConflictError, match="allow_destructive"):
        await enqueue_retry_forward(
            namespace=saga.namespace,
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
            allow_destructive=False,
        )


@pytest.mark.asyncio
async def test_retry_forward_schedules_when_on_hold(mocker):
    saga, steps = await create_saga_with_steps(step_count=2)
    step = steps[1]
    step.status = StepStatus.FAILED
    step.error_details = {"code": "TOOL_ERROR", "message": "boom"}
    step.on_failure_strategy = "await_operator"
    await step.save()
    saga.status = SagaStatus.AWAITING_RECOVERY
    await saga.save()

    trigger = mocker.patch(
        "engine.recovery.trigger_step",
        new_callable=AsyncMock,
    )
    result = await enqueue_retry_forward(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )
    assert result["status"] == "scheduled"
    await saga.refresh_from_db()
    assert saga.status == SagaStatus.RUNNING
    trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_compensation_from_hold(mocker):
    saga, steps = await create_saga_with_steps(step_count=2)
    step = steps[1]
    step.status = StepStatus.FAILED
    step.error_details = {"code": "TOOL_ERROR", "message": "boom"}
    await step.save()
    saga.status = SagaStatus.AWAITING_RECOVERY
    await saga.save()

    compensate = mocker.patch(
        "engine.recovery.trigger_compensation",
        new_callable=AsyncMock,
    )
    result = await enqueue_start_compensation(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )
    assert result["status"] == "scheduled"
    await saga.refresh_from_db()
    assert saga.status == SagaStatus.COMPENSATING
    compensate.assert_awaited_once()
    assert compensate.await_args.kwargs["forward_seq"] == step.forward_seq - 1


async def _hold_at_awaiting_recovery(
    *,
    step_count: int = 1,
    fail_index: int = 0,
    step_status: StepStatus = StepStatus.FAILED,
    error_details: dict | None = None,
):
    saga, steps = await create_saga_with_steps(step_count=step_count)
    for i, s in enumerate(steps):
        if i < fail_index:
            s.status = StepStatus.COMPLETED
            s.output_payload = {"data": {"ok": True}}
        elif i == fail_index:
            s.status = step_status
            s.on_failure_strategy = "await_operator"
            s.error_details = error_details or {"code": "TOOL_ERROR", "message": "boom"}
        else:
            s.status = StepStatus.PENDING
        await s.save()
    saga.status = SagaStatus.AWAITING_RECOVERY
    await saga.save()
    return saga, steps


@pytest.mark.asyncio
async def test_retry_forward_then_step_completed_continues_forward(mocker):
    """1. Happy path: hold → retry-forward → STEP_COMPLETED advances the graph."""
    saga, steps = await _hold_at_awaiting_recovery(step_count=2, fail_index=0)
    step0, step1 = steps[0], steps[1]
    prior_key = step0.idempotency_key

    async def _mark_in_progress(*_a, **_k):
        await step0.refresh_from_db()
        step0.status = StepStatus.IN_PROGRESS
        await step0.save()

    mocker.patch("engine.recovery.trigger_step", new=AsyncMock(side_effect=_mark_in_progress))
    result = await enqueue_retry_forward(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step0.span_id,
    )
    assert result["status"] == "scheduled"
    await step0.refresh_from_db()
    assert step0.idempotency_key != prior_key
    assert step0.status == StepStatus.IN_PROGRESS

    trigger_next = mocker.patch("engine.logic.trigger_step", new_callable=AsyncMock)
    await process_saga_event(
        {
            "event_type": EventType.STEP_COMPLETED.value,
            "saga_trace_id": saga.trace_id,
            "namespace": saga.namespace,
            "step_span_id": step0.span_id,
            "output": {"data": {"ok": True}},
        }
    )

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    assert step0.status == StepStatus.COMPLETED
    assert saga.status == SagaStatus.RUNNING
    trigger_next.assert_awaited()
    assert trigger_next.await_args.args[1] == step1.forward_seq


@pytest.mark.asyncio
async def test_retry_forward_dirty_with_allow_destructive_schedules(mocker):
    """2B. Dirty hold may retry when allow_destructive=true."""
    saga, steps = await _hold_at_awaiting_recovery(
        step_status=StepStatus.TIMED_OUT,
        error_details={"code": "TIMEOUT", "message": "timed out"},
    )
    step = steps[0]
    trigger = mocker.patch("engine.recovery.trigger_step", new_callable=AsyncMock)
    result = await enqueue_retry_forward(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
        allow_destructive=True,
    )
    assert result["status"] == "scheduled"
    await saga.refresh_from_db()
    assert saga.status == SagaStatus.RUNNING
    trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_forward_rotates_command_key_but_keeps_stable_tool_key(mocker):
    """3. Command idempotency_key rotates; fwd-{trace}-{span} tool key stays put."""
    saga, steps = await _hold_at_awaiting_recovery()
    step = steps[0]
    stable = forward_tool_idempotency_key(trace_id=saga.trace_id, span_id=step.span_id)
    prior_cmd = step.idempotency_key
    mocker.patch("engine.recovery.trigger_step", new_callable=AsyncMock)

    await enqueue_retry_forward(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=step.span_id,
    )

    await step.refresh_from_db()
    assert step.idempotency_key != prior_cmd
    assert forward_tool_idempotency_key(trace_id=saga.trace_id, span_id=step.span_id) == stable
    assert stable == f"fwd-{saga.trace_id}-{step.span_id}"


@pytest.mark.asyncio
async def test_late_worker_results_ignored_while_awaiting_recovery():
    """4. Late STEP_COMPLETED / STEP_FAILED while held must not leave AWAITING_RECOVERY."""
    saga, steps = await _hold_at_awaiting_recovery()
    step = steps[0]
    fail_key = f"{saga.trace_id}:{EventType.STEP_FAILED.value}:{step.span_id}"
    await ProcessedIngestEvent.create(event_dedup_key=fail_key)

    await process_saga_event(
        {
            "event_type": EventType.STEP_COMPLETED.value,
            "saga_trace_id": saga.trace_id,
            "namespace": saga.namespace,
            "step_span_id": step.span_id,
            "output": {"data": {"late": True}},
        }
    )
    await saga.refresh_from_db()
    await step.refresh_from_db()
    assert saga.status == SagaStatus.AWAITING_RECOVERY
    assert step.status == StepStatus.FAILED

    await ProcessedIngestEvent.filter(event_dedup_key=fail_key).delete()
    await process_saga_event(
        {
            "event_type": EventType.STEP_FAILED.value,
            "saga_trace_id": saga.trace_id,
            "namespace": saga.namespace,
            "step_span_id": step.span_id,
            "error_details": {"code": "LATE", "message": "stale failure"},
            "output": {"code": "LATE"},
        }
    )
    await saga.refresh_from_db()
    await step.refresh_from_db()
    assert saga.status == SagaStatus.AWAITING_RECOVERY
    assert step.status == StepStatus.FAILED
    assert step.error_details == {"code": "TOOL_ERROR", "message": "boom"}


@pytest.mark.asyncio
async def test_start_compensation_three_step_lifo_starts_at_prior_completed(mocker):
    """5. After hold on step 3, start-compensation unwinds from step 2 (LIFO)."""
    saga, steps = await _hold_at_awaiting_recovery(step_count=3, fail_index=2)
    failed = steps[2]
    assert failed.forward_seq == 2

    compensate = mocker.patch(
        "engine.recovery.trigger_compensation",
        new_callable=AsyncMock,
    )
    result = await enqueue_start_compensation(
        namespace=saga.namespace,
        trace_id=saga.trace_id,
        step_span_id=failed.span_id,
    )
    assert result["status"] == "scheduled"
    await saga.refresh_from_db()
    assert saga.status == SagaStatus.COMPENSATING
    # Clean failure: undo window starts at forward_seq-1 (step 2), then step 1.
    assert compensate.await_args.kwargs["forward_seq"] == 1


@pytest.mark.asyncio
async def test_consecutive_awaiting_recovery_cycles_clear_outbox_each_time(mocker):
    """6. Multiple fail→hold→retry cycles re-emit STEP_FAILED without unique collisions."""
    saga, steps = await _hold_at_awaiting_recovery()
    step = steps[0]
    key = f"{saga.trace_id}:{EventType.STEP_FAILED.value}:{step.span_id}"

    async def _seed_worker_step_failed_outbox(*, label: str) -> None:
        """Simulate worker outbox row after a failure (ingest may already exist)."""
        if not await OutboxEvent.filter(idempotency_key=key).exists():
            await emit_saga_event(
                topic=TOPIC_ORCHESTRATOR_EVENTS,
                event_type=EventType.STEP_FAILED.value,
                payload_schema=StepFailedResultEvent(
                    namespace=saga.namespace,
                    saga_trace_id=saga.trace_id,
                    step_span_id=step.span_id,
                    output={"error": label},
                    error_details={"error": label},
                ),
            )
        if not await ProcessedIngestEvent.filter(event_dedup_key=key).exists():
            await ProcessedIngestEvent.create(event_dedup_key=key)
        row = await OutboxEvent.filter(idempotency_key=key).first()
        assert row is not None
        row.status = OutboxStatus.COMPLETED
        await row.save()

    await _seed_worker_step_failed_outbox(label="first")
    mocker.patch("engine.recovery.trigger_step", new_callable=AsyncMock)

    for attempt in ("second", "third"):
        await enqueue_retry_forward(
            namespace=saga.namespace,
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
        )
        assert not await OutboxEvent.filter(idempotency_key=key).exists()
        assert not await ProcessedIngestEvent.filter(event_dedup_key=key).exists()

        # Simulate worker failure again while operator has not yet chosen compensate.
        step.status = StepStatus.IN_PROGRESS
        step.on_failure_strategy = "await_operator"
        await step.save()
        saga.status = SagaStatus.RUNNING
        await saga.save()
        await process_saga_event(
            {
                "event_type": EventType.STEP_FAILED.value,
                "saga_trace_id": saga.trace_id,
                "namespace": saga.namespace,
                "step_span_id": step.span_id,
                "error_details": {"code": "TOOL_ERROR", "message": attempt},
                "output": {"error": attempt},
            }
        )
        await saga.refresh_from_db()
        await step.refresh_from_db()
        assert saga.status == SagaStatus.AWAITING_RECOVERY
        assert step.status == StepStatus.FAILED

        await _seed_worker_step_failed_outbox(label=attempt)

    # Final clear still allows a fresh emit (no unique-constraint deadlock).
    async with in_transaction() as conn:
        await _clear_worker_result_ingest_dedup(
            saga.trace_id,
            step.span_id,
            namespace=saga.namespace,
            db_conn=conn,
        )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type=EventType.STEP_FAILED.value,
        payload_schema=StepFailedResultEvent(
            namespace=saga.namespace,
            saga_trace_id=saga.trace_id,
            step_span_id=step.span_id,
            output={"error": "final"},
            error_details={"error": "final"},
        ),
    )
    rows = await OutboxEvent.filter(idempotency_key=key).all()
    assert len(rows) == 1
    assert rows[0].payload.get("output") == {"error": "final"}
