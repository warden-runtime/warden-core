"""Operator-initiated saga step recovery (retry forward step or compensation)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from common.command_specs import slim_tool_specs
from common.compensation_context import (
    compensation_parameter_context,
    worker_snapshot_for_compensation,
)
from common.contracts import CommandType, DoCompensationCommand, EventType
from common.models import (
    OutboxEvent,
    OutboxStatus,
    ProcessedCommand,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
    WorkerDefinition,
)
from common.outbox import emit_saga_event
from common.outbox_reap import stale_outbox_cutoff
from common.outbox_timestamps import utc_now
from common.plugins.registry import get_registry
from common.processed_command_reap import (
    claim_is_stale,
    release_worker_claim_for_retry,
)
from common.topics import TOPIC_WORKER_COMMANDS
from common.worker_ref import resolve_worker_from_compensation
from pydantic import ValidationError

if TYPE_CHECKING:
    from common.resource_specs import ResourceSpec
    from tortoise.backends.base.client import BaseDBAsyncClient

from engine.execution_timing import clear_step_timing_fields
from engine.logic import (
    _clear_worker_result_ingest_dedup,
    _reset_step_for_worker_retry,
    trigger_step,
)
from engine.recovery_errors import (
    RecoveryClaimActiveError,
    RecoveryConflictError,
    RecoveryNotFoundError,
)
from engine.recovery_idempotency import with_operator_recovery_idempotency
from engine.utils import resolve_parameters_spec

logger = logging.getLogger(__name__)


def _outbox_row_stale(row: OutboxEvent, *, cutoff: datetime | None = None) -> bool:
    threshold = cutoff if cutoff is not None else stale_outbox_cutoff()
    updated = row.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return updated < threshold


async def _load_saga_step_locked(
    *,
    namespace: str,
    trace_id: str,
    step_span_id: str,
    conn: BaseDBAsyncClient,
) -> tuple[SagaInstance, SagaStepInstance]:
    saga = (
        await SagaInstance.filter(trace_id=trace_id, namespace=namespace)
        .using_db(conn)
        .select_for_update()
        .first()
    )
    if saga is None:
        raise RecoveryNotFoundError(f"Saga {trace_id} not found.")
    step = (
        await SagaStepInstance.filter(
            span_id=step_span_id,
            saga_trace_id=trace_id,
            namespace=namespace,
        )
        .using_db(conn)
        .select_for_update()
        .first()
    )
    if step is None:
        raise RecoveryNotFoundError(f"Step {step_span_id} not found.")
    return saga, step


def _enforce_commit_force_gating(
    *,
    step: SagaStepInstance,
    force: bool,
    allow_destructive: bool,
) -> None:
    if force and step.step_kind == "commit" and not allow_destructive:
        raise RecoveryConflictError(
            "force retry on commit steps requires allow_destructive=true "
            "(risk of duplicate side effects)."
        )


async def _handle_active_claim(
    *,
    idempotency_key: str,
    force: bool,
    conn: BaseDBAsyncClient,
) -> None:
    row = await ProcessedCommand.filter(idempotency_key=idempotency_key).using_db(conn).first()
    if row is None or row.result_emitted:
        return
    if claim_is_stale(row) or force:
        await release_worker_claim_for_retry(idempotency_key, conn=conn)
        if force:
            logger.warning(
                "Operator force released ProcessedCommand idempotency_key=%s",
                idempotency_key,
            )
        return
    raise RecoveryClaimActiveError(
        idempotency_key=idempotency_key,
    )


async def _requeue_worker_outbox(
    *,
    namespace: str,
    idempotency_key: str,
    conn: BaseDBAsyncClient,
    force: bool,
) -> bool:
    row = (
        await OutboxEvent.filter(
            namespace=namespace,
            destination_topic=TOPIC_WORKER_COMMANDS,
            idempotency_key=idempotency_key,
        )
        .using_db(conn)
        .first()
    )
    if row is None:
        return False
    if row.status == OutboxStatus.PENDING:
        return True
    if row.status == OutboxStatus.FAILED:
        row.status = OutboxStatus.PENDING
        row.updated_at = utc_now()
        await row.save(using_db=conn, update_fields=["status", "updated_at"])
        return True
    if row.status == OutboxStatus.IN_PROGRESS and (force or _outbox_row_stale(row)):
        row.status = OutboxStatus.PENDING
        row.updated_at = utc_now()
        await row.save(using_db=conn, update_fields=["status", "updated_at"])
        return True
    return row.status == OutboxStatus.PENDING


async def _clear_compensation_ingest_dedup(
    saga_trace_id: str,
    step_span_id: str,
    *,
    conn: BaseDBAsyncClient,
) -> None:
    from common.models import ProcessedIngestEvent

    keys = [
        f"{saga_trace_id}:{EventType.STEP_COMPENSATED.value}:{step_span_id}",
        f"{saga_trace_id}:{EventType.COMPENSATION_FAILED.value}:{step_span_id}",
    ]
    await ProcessedIngestEvent.filter(event_dedup_key__in=keys).using_db(conn).delete()


def _normalize_spec_list(items: object, *, fallback_key: str) -> list[dict]:
    if not isinstance(items, list):
        return []
    return [s if isinstance(s, dict) else {fallback_key: str(s)} for s in items]


def _compensation_tool_resource_specs(
    comp_def: dict,
) -> tuple[list[dict], list[dict]]:
    comp_tools = comp_def.get("tools") or {}
    tool_allow = comp_tools.get("allow", []) if isinstance(comp_tools, dict) else []
    comp_resources = comp_def.get("resources") or {}
    resource_allow = comp_resources.get("allow", []) if isinstance(comp_resources, dict) else []
    return (
        _normalize_spec_list(tool_allow, fallback_key="name"),
        _normalize_spec_list(resource_allow, fallback_key="uri"),
    )


async def _build_compensation_command_for_step(
    *,
    saga: SagaInstance,
    comp_step: SagaStepInstance,
    forward: SagaStepInstance,
) -> DoCompensationCommand:
    comp_def = forward.compensation_definition or {}
    with_spec = comp_def.get("with") or {}
    resolve_ctx = compensation_parameter_context(
        saga,
        forward,
        undo_span_id=comp_step.span_id,
        idempotency_key=comp_step.idempotency_key,
    )
    resolved_comp_input = resolve_parameters_spec(with_spec, resolve_ctx)
    comp_worker, comp_worker_version = resolve_worker_from_compensation(
        comp_def,
        forward_worker=forward.worker,
        forward_worker_version=forward.worker_version,
    )
    comp_tool_specs, comp_resource_specs = _compensation_tool_resource_specs(comp_def)
    worker_row = await WorkerDefinition.get_or_none(
        name=comp_worker,
        namespace=saga.namespace,
        version=comp_worker_version,
    )
    worker_snapshot = worker_snapshot_for_compensation(worker_row) if worker_row else None

    return DoCompensationCommand(
        type=CommandType.EXECUTE_COMPENSATION,
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=comp_step.span_id,
        worker_name=comp_worker,
        worker_version=comp_worker_version,
        idempotency_key=comp_step.idempotency_key,
        forward_step_span_id=forward.span_id,
        original_input=resolved_comp_input,
        failure_reason=forward.error_details,
        tool_specs=slim_tool_specs(comp_tool_specs),
        resource_specs=cast("list[ResourceSpec]", comp_resource_specs),
        worker_snapshot=worker_snapshot,
    )


async def _emit_compensation_retry(
    *,
    saga: SagaInstance,
    comp_step: SagaStepInstance,
    forward: SagaStepInstance,
    conn: BaseDBAsyncClient,
) -> None:
    try:
        command = await _build_compensation_command_for_step(
            saga=saga,
            comp_step=comp_step,
            forward=forward,
        )
    except (ValidationError, ValueError) as exc:
        raise RecoveryConflictError(f"Cannot rebuild compensation command: {exc}") from exc
    await emit_saga_event(
        topic=TOPIC_WORKER_COMMANDS,
        event_type=CommandType.EXECUTE_COMPENSATION.value,
        payload_schema=command,
        conn=conn,
    )


async def _execute_step_retry(
    conn: BaseDBAsyncClient,
    *,
    namespace: str,
    trace_id: str,
    step_span_id: str,
    token: str,
    force: bool,
    allow_destructive: bool,
    reason: str | None,
) -> dict[str, str]:
    saga, step = await _load_saga_step_locked(
        namespace=namespace,
        trace_id=trace_id,
        step_span_id=step_span_id,
        conn=conn,
    )
    if saga.status != SagaStatus.RUNNING:
        raise RecoveryConflictError(
            f"Saga status is {saga.status}; expected RUNNING for retry-step."
        )
    if step.compensates_span_id is not None:
        raise RecoveryConflictError("retry-step applies to forward steps only.")
    if step.status != StepStatus.IN_PROGRESS:
        raise RecoveryConflictError(
            f"Step status is {step.status}; expected IN_PROGRESS for retry-step."
        )
    _enforce_commit_force_gating(
        step=step,
        force=force,
        allow_destructive=allow_destructive,
    )
    prior_key = step.idempotency_key
    try:
        await _handle_active_claim(idempotency_key=prior_key, force=force, conn=conn)
    except RecoveryClaimActiveError:
        return {
            "status": "claim_active",
            "idempotency_key": prior_key,
            "worker_command_key": prior_key,
            "recovery_token": token,
        }
    requeued = await _requeue_worker_outbox(
        namespace=namespace,
        idempotency_key=prior_key,
        conn=conn,
        force=force,
    )
    if requeued:
        step.output_payload = None
        step.error_details = None
        step.pending_review_payload = None
        clear_step_timing_fields(step)
        await step.save(using_db=conn)
        await _clear_worker_result_ingest_dedup(
            trace_id,
            step_span_id,
            db_conn=conn,
        )
        worker_key = prior_key
    else:
        await _reset_step_for_worker_retry(saga, step, saga.trace_id, db_conn=conn)
        await trigger_step(
            saga,
            step.order_index,
            conn,
            allow_retry_in_progress=True,
        )
        worker_key = step.idempotency_key
    await get_registry().engine.on_operator_recovery_requested(
        saga=saga,
        step=step,
        recovery_kind="retry-step",
        conn=conn,
        force=force,
        allow_destructive=allow_destructive,
        recovery_token=token,
        reason=reason,
        prior_idempotency_key=prior_key,
        new_idempotency_key=step.idempotency_key,
    )
    status = "requeued" if requeued else "scheduled"
    return {
        "status": status,
        "idempotency_key": worker_key,
        "worker_command_key": worker_key,
        "recovery_token": token,
    }


async def enqueue_step_retry(
    *,
    namespace: str,
    trace_id: str,
    step_span_id: str,
    recovery_token: str | None = None,
    force: bool = False,
    allow_destructive: bool = False,
    reason: str | None = None,
) -> dict[str, str]:
    """Retry a stuck forward step (RUNNING saga, IN_PROGRESS forward step)."""
    token = recovery_token or uuid.uuid4().hex

    async def apply(conn: BaseDBAsyncClient) -> dict[str, str]:
        return await _execute_step_retry(
            conn,
            namespace=namespace,
            trace_id=trace_id,
            step_span_id=step_span_id,
            token=token,
            force=force,
            allow_destructive=allow_destructive,
            reason=reason,
        )

    return await with_operator_recovery_idempotency(
        recovery_token=recovery_token,
        namespace=namespace,
        recovery_kind="retry-step",
        trace_id=trace_id,
        step_span_id=step_span_id,
        force=force,
        allow_destructive=allow_destructive,
        apply=apply,
    )


async def _execute_compensation_retry(
    conn: BaseDBAsyncClient,
    *,
    namespace: str,
    trace_id: str,
    step_span_id: str,
    token: str,
    force: bool,
    reason: str | None,
) -> dict[str, str]:
    saga, comp_step = await _load_saga_step_locked(
        namespace=namespace,
        trace_id=trace_id,
        step_span_id=step_span_id,
        conn=conn,
    )
    if comp_step.compensates_span_id is None:
        raise RecoveryConflictError("retry-compensation applies to compensation steps only.")
    if saga.status not in (SagaStatus.COMPENSATING, SagaStatus.FAILED):
        raise RecoveryConflictError(
            f"Saga status is {saga.status}; expected COMPENSATING or FAILED."
        )
    if comp_step.status not in (
        StepStatus.FAILED,
        StepStatus.IN_PROGRESS,
        StepStatus.COMPENSATING,
    ):
        raise RecoveryConflictError(
            f"Step status is {comp_step.status}; expected FAILED, IN_PROGRESS, or COMPENSATING."
        )
    forward = (
        await SagaStepInstance.filter(
            span_id=comp_step.compensates_span_id,
            saga_trace_id=trace_id,
        )
        .using_db(conn)
        .first()
    )
    if forward is None:
        raise RecoveryNotFoundError("Forward step for compensation not found.")
    if saga.status == SagaStatus.FAILED:
        saga.status = SagaStatus.COMPENSATING
        await saga.save(using_db=conn)
    comp_step.status = StepStatus.COMPENSATING
    comp_step.error_details = None
    comp_step.end_time = None
    await comp_step.save(using_db=conn)
    await _clear_compensation_ingest_dedup(trace_id, step_span_id, conn=conn)
    await _handle_active_claim(
        idempotency_key=comp_step.idempotency_key,
        force=force,
        conn=conn,
    )
    requeued = await _requeue_worker_outbox(
        namespace=namespace,
        idempotency_key=comp_step.idempotency_key,
        conn=conn,
        force=force,
    )
    if not requeued:
        await _emit_compensation_retry(
            saga=saga,
            comp_step=comp_step,
            forward=forward,
            conn=conn,
        )
    await get_registry().engine.on_operator_recovery_requested(
        saga=saga,
        step=comp_step,
        recovery_kind="retry-compensation",
        conn=conn,
        force=force,
        recovery_token=token,
        reason=reason,
    )
    return {
        "status": "requeued" if requeued else "scheduled",
        "idempotency_key": comp_step.idempotency_key,
        "worker_command_key": comp_step.idempotency_key,
        "recovery_token": token,
    }


async def enqueue_compensation_retry(
    *,
    namespace: str,
    trace_id: str,
    step_span_id: str,
    recovery_token: str | None = None,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, str]:
    """Retry a failed or stuck compensation step."""
    token = recovery_token or uuid.uuid4().hex

    async def apply(conn: BaseDBAsyncClient) -> dict[str, str]:
        return await _execute_compensation_retry(
            conn,
            namespace=namespace,
            trace_id=trace_id,
            step_span_id=step_span_id,
            token=token,
            force=force,
            reason=reason,
        )

    return await with_operator_recovery_idempotency(
        recovery_token=recovery_token,
        namespace=namespace,
        recovery_kind="retry-compensation",
        trace_id=trace_id,
        step_span_id=step_span_id,
        force=force,
        allow_destructive=None,
        apply=apply,
    )
