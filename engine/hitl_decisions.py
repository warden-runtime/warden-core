"""HITL human approve/reject/retry: validate step state and enqueue orchestrator outbox events."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from common.contracts import (
    HumanApprovedOutboxPayload,
    HumanRejectedOutboxPayload,
    HumanRetryOutboxPayload,
)
from common.hitl_retry import HitlRetryLimitError, assert_hitl_retry_allowed
from common.models import EventType, OutboxEvent, OutboxStatus, SagaStepInstance, StepStatus
from common.outbox import emit_saga_event
from common.outbox_timestamps import utc_now
from common.plugins.registry import get_registry
from common.topics import TOPIC_ORCHESTRATOR_EVENTS
from common.utils import hash_canonical_dict
from tortoise.transactions import in_transaction

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient


class HumanDecisionNotFoundError(Exception):
    """No step exists for the given saga trace and span id."""


class HumanDecisionConflictError(Exception):
    """Step is not in AWAITING_HUMAN."""

    def __init__(self, *, status: str) -> None:
        self.status = status
        super().__init__(f"Step is not awaiting human review (status={status}).")


class HumanRetryLimitError(HitlRetryLimitError):
    """Manual HITL retry limit exhausted for this step."""


class InvalidHumanDecisionError(Exception):
    """Decision value is not APPROVE or REJECT."""

    def __init__(self, decision: str) -> None:
        self.decision = decision
        super().__init__(f"decision must be APPROVE or REJECT (got {decision!r}).")


def human_decision_idempotency_key(*, trace_id: str, step_span_id: str) -> str:
    return f"human-decision-{trace_id}-{step_span_id}"


def human_retry_outbox_idempotency_key(
    *,
    trace_id: str,
    step_span_id: str,
    retry_token: str,
) -> str:
    """Outbox idempotency for a single retry HTTP request (not a cap on retry count)."""
    return f"human-retry:{trace_id}:{step_span_id}:{retry_token}"


def _rejection_reason(error_details: dict | None) -> str | None:
    if not error_details:
        return None
    msg = error_details.get("message") if isinstance(error_details, dict) else None
    return str(msg) if msg is not None else None


def _approval_output_hash(output: dict | None) -> str | None:
    if output is None or not isinstance(output, dict):
        return None
    return hash_canonical_dict(output)


def _resolve_decision_event(
    *,
    decision: str,
    namespace: str,
    trace_id: str,
    step_span_id: str,
    decision_key: str,
    output: dict | None,
    error_details: dict | None,
) -> tuple[str, HumanApprovedOutboxPayload | HumanRejectedOutboxPayload]:
    if decision == "APPROVE":
        payload = HumanApprovedOutboxPayload(
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=step_span_id,
            idempotency_key=decision_key,
            output=output,
        )
        return EventType.HUMAN_APPROVED.value, payload
    if decision == "REJECT":
        payload = HumanRejectedOutboxPayload(
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=step_span_id,
            idempotency_key=decision_key,
            error_details=error_details,
        )
        return EventType.HUMAN_REJECTED.value, payload
    raise InvalidHumanDecisionError(decision)


async def _load_step_for_decision(
    *,
    conn: BaseDBAsyncClient,
    namespace: str,
    trace_id: str,
    step_span_id: str,
) -> SagaStepInstance:
    step = (
        await SagaStepInstance.filter(
            namespace=namespace,
            saga_trace_id=trace_id,
            span_id=step_span_id,
            compensates_span_id__isnull=True,
        )
        .using_db(conn)
        .select_for_update()
        .first()
    )
    if step is None:
        raise HumanDecisionNotFoundError()
    if step.status != StepStatus.AWAITING_HUMAN:
        raise HumanDecisionConflictError(status=str(step.status))
    return step


async def _emit_human_decision(
    *,
    conn: BaseDBAsyncClient,
    step: SagaStepInstance,
    namespace: str,
    trace_id: str,
    step_span_id: str,
    decision: str,
    decision_key: str,
    output: dict | None,
    error_details: dict | None,
) -> None:
    event_type, payload = _resolve_decision_event(
        decision=decision,
        namespace=namespace,
        trace_id=trace_id,
        step_span_id=step_span_id,
        decision_key=decision_key,
        output=output,
        error_details=error_details,
    )
    output_hash = _approval_output_hash(output) if decision == "APPROVE" else None
    rejection_reason = _rejection_reason(error_details) if decision == "REJECT" else None

    await get_registry().engine.on_hitl_decision_queued(
        saga=None,
        step=step,
        decision=decision,
        conn=conn,
        namespace=namespace,
        saga_trace_id=trace_id,
        step_span_id=step_span_id,
        idempotency_key=decision_key,
        output_hash=output_hash,
        rejection_reason=rejection_reason,
    )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type=event_type,
        payload_schema=payload,
        conn=conn,
    )


async def enqueue_human_decision(
    *,
    trace_id: str,
    step_span_id: str,
    namespace: str,
    decision: str,
    output: dict | None = None,
    error_details: dict | None = None,
) -> dict[str, str]:
    """Queue HUMAN_APPROVED or HUMAN_REJECTED for a step held in AWAITING_HUMAN."""
    decision_key = human_decision_idempotency_key(
        trace_id=trace_id,
        step_span_id=step_span_id,
    )
    async with in_transaction() as conn:
        existing = (
            await OutboxEvent.filter(
                destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
                idempotency_key=decision_key,
            )
            .using_db(conn)
            .first()
        )
        if existing is not None:
            if existing.status == OutboxStatus.FAILED:
                existing.status = OutboxStatus.PENDING
                existing.updated_at = utc_now()
                await existing.save(using_db=conn, update_fields=["status", "updated_at"])
                return {"status": "requeued", "idempotency_key": decision_key}
            return {"status": "already_queued", "idempotency_key": decision_key}

        step = await _load_step_for_decision(
            conn=conn,
            namespace=namespace,
            trace_id=trace_id,
            step_span_id=step_span_id,
        )
        await _emit_human_decision(
            conn=conn,
            step=step,
            namespace=namespace,
            trace_id=trace_id,
            step_span_id=step_span_id,
            decision=decision,
            decision_key=decision_key,
            output=output,
            error_details=error_details,
        )
    return {"status": "queued", "idempotency_key": decision_key}


async def enqueue_hitl_retry(
    *,
    trace_id: str,
    step_span_id: str,
    namespace: str,
    retry_token: str | None = None,
    retry_guidance: str | None = None,
) -> dict[str, str]:
    """Queue HUMAN_RETRY for a step held in AWAITING_HUMAN (new token per request unless supplied)."""
    token = retry_token or uuid.uuid4().hex
    retry_key = human_retry_outbox_idempotency_key(
        trace_id=trace_id,
        step_span_id=step_span_id,
        retry_token=token,
    )
    async with in_transaction() as conn:
        existing = (
            await OutboxEvent.filter(
                destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
                idempotency_key=retry_key,
            )
            .using_db(conn)
            .first()
        )
        if existing is not None:
            if existing.status == OutboxStatus.FAILED:
                existing.status = OutboxStatus.PENDING
                existing.updated_at = utc_now()
                await existing.save(using_db=conn, update_fields=["status", "updated_at"])
                return {
                    "status": "requeued",
                    "idempotency_key": retry_key,
                    "retry_token": token,
                }
            return {"status": "already_queued", "idempotency_key": retry_key}

        step = await _load_step_for_decision(
            conn=conn,
            namespace=namespace,
            trace_id=trace_id,
            step_span_id=step_span_id,
        )
        assert_hitl_retry_allowed(step)
        guidance = retry_guidance.strip() if isinstance(retry_guidance, str) else None
        if guidance == "":
            guidance = None
        await get_registry().engine.on_hitl_retry_queued(
            saga=None,
            step=step,
            conn=conn,
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=step_span_id,
            idempotency_key=retry_key,
            retry_token=token,
            retry_guidance=guidance,
            attempt_after=int(step.hitl_retry_count) + 1,
            max_retries=step.hitl_max_retries,
        )
        payload = HumanRetryOutboxPayload(
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=step_span_id,
            idempotency_key=retry_key,
            retry_guidance=guidance,
        )
        await emit_saga_event(
            topic=TOPIC_ORCHESTRATOR_EVENTS,
            event_type=EventType.HUMAN_RETRY.value,
            payload_schema=payload,
            conn=conn,
        )
    return {"status": "queued", "idempotency_key": retry_key, "retry_token": token}
