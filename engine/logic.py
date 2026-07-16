import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from common.command_specs import slim_tool_specs
from common.compensation import (
    compensation_tool_resource_specs,
    forward_eligible_for_compensation,
    forward_step_has_compensation,
)
from common.compensation_context import (
    compensation_parameter_context,
    step_output_for_saga_context,
    worker_snapshot_for_compensation,
)
from common.config import get_settings
from common.contracts import (
    CommandType,
    CompensationFailedIngestEvent,
    DoCommitCommand,
    DoCompensationCommand,
    DoStepCommand,
    HumanApprovedIngestEvent,
    HumanRejectedIngestEvent,
    HumanRetryIngestEvent,
    SagaEventPayload,
    SagaIngestEvent,
    SagaStartedEvent,
    StepCompensatedIngestEvent,
    StepCompletedIngestEvent,
    StepFailedEvent,
    coerce_saga_ingest_dict,
)
from common.exceptions import UnrecoverableError
from common.execution_timing import EngineTimingAccumulator, elapsed_ms
from common.hitl_retry import (
    HitlRetryLimitError,
    assert_hitl_retry_allowed,
    merge_hitl_retry_into_arguments,
)
from common.models import (
    EventType,
    ProcessedIngestEvent,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
    WorkerDefinition,
)
from common.outbox import emit_saga_event
from common.plugins.registry import get_registry
from common.policy_gate import PolicyGateOutcome, run_policy_gate
from common.processed_command_reap import release_worker_claim_for_retry
from common.prompts import assert_prompt_file_exists
from common.schemas.engine_events import AuditEngineEventType
from common.schemas.saga import SAGA_STEP_KINDS
from common.step_output import (
    step_context_entry_for_saga,
    validate_business_data_schema,
)
from common.step_when import (
    PolicyEvaluationError,
    evaluate_step_when,
    step_when_binding,
)
from common.telemetry import trace_boundary, trace_step
from common.topics import TOPIC_ORCHESTRATOR_EVENTS, TOPIC_WORKER_COMMANDS
from common.utils import coerce_dict, hash_canonical_dict, status_value
from common.worker_ref import resolve_worker_from_compensation
from opentelemetry import trace
from pydantic import TypeAdapter, ValidationError
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import DoesNotExist, IntegrityError
from tortoise.query_utils import Prefetch
from tortoise.transactions import in_transaction

from engine.execution_timing import (
    add_engine_bucket_ms,
    clear_step_timing_fields,
    finalize_step_execution_timing,
    merge_step_timing_if_needed,
    persist_pending_engine_timing,
    persist_schedule_engine_timing_on_policy_denial,
)
from engine.execution_usage import (
    finalize_step_execution_usage,
    merge_step_usage_if_needed,
)
from engine.utils import resolve_parameters_spec

if TYPE_CHECKING:
    from common.resource_specs import ResourceSpec


async def _forward_step_count(*, saga_trace_id: str, db_conn: BaseDBAsyncClient) -> int:
    """Number of forward (blueprint) step rows for this saga."""
    return await (
        SagaStepInstance.filter(saga_trace_id=saga_trace_id, compensates_span_id__isnull=True)
        .using_db(db_conn)
        .count()
    )


async def _mark_step_skipped_when_false(
    saga: SagaInstance,
    step: SagaStepInstance,
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    when_cel_ms: int = 0,
) -> None:
    """Mark a forward step SKIPPED because ``when.cel`` evaluated to false."""
    from_status = status_value(step.status)
    step.status = StepStatus.SKIPPED
    step.end_time = datetime.now(UTC)
    if when_cel_ms > 0:
        from common.execution_timing import merge_execution_timing

        step.execution_timing = merge_execution_timing(engine={"when_cel_ms": when_cel_ms})
    await step.save(using_db=db_conn)
    await get_registry().engine.on_step_transition(
        saga=saga,
        step=step,
        from_status=from_status,
        to_status=status_value(StepStatus.SKIPPED),
        conn=db_conn,
        trace_context=trace_context,
        reason="when_false",
    )


async def _notify_when_skipped_summary(
    saga: SagaInstance,
    *,
    skipped_count: int,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
) -> None:
    if skipped_count <= 0:
        return
    await get_registry().engine.on_steps_skipped_summary(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        skipped_count=skipped_count,
        conn=db_conn,
        trace_context=trace_context,
        saga=saga,
        reason="when_false",
    )


async def _schedule_next_forward_step(
    saga: SagaInstance,
    after_order: int,
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Schedule the next eligible forward step, skipping rows whose ``when.cel`` is false."""
    forward_count = await _forward_step_count(saga_trace_id=saga.trace_id, db_conn=db_conn)
    skipped_count = 0
    for order in range(after_order + 1, forward_count):
        step = (
            await SagaStepInstance.filter(
                saga_trace_id=saga.trace_id,
                order_index=order,
                compensates_span_id__isnull=True,
            )
            .using_db(db_conn)
            .select_for_update()
            .first()
        )
        if not step:
            logger.error(
                "Saga %s missing forward step at order %s during schedule.",
                saga.trace_id,
                order,
            )
            return

        when_cel = (step.when_cel or "").strip()
        when_ms = 0
        if when_cel:
            when_start = time.perf_counter()
            try:
                should_run = evaluate_step_when(
                    cel_source=when_cel,
                    binding=step_when_binding(saga=saga, step=step),
                )
            except PolicyEvaluationError as e:
                when_ms = elapsed_ms(when_start)
                synthetic = StepFailedEvent(
                    saga_trace_id=saga.trace_id,
                    namespace=saga.namespace,
                    event_type=EventType.STEP_FAILED.value,
                    step_span_id=step.span_id,
                    error_details={
                        "code": "WHEN_EVALUATION_FAILED",
                        "message": str(e),
                    },
                )
                if when_ms > 0:
                    from common.execution_timing import merge_execution_timing

                    step.execution_timing = merge_execution_timing(engine={"when_cel_ms": when_ms})
                    await step.save(using_db=db_conn, update_fields=["execution_timing"])
                await _apply_step_failure_lifecycle(
                    saga,
                    step,
                    synthetic,
                    db_conn,
                    trace_context=trace_context,
                )
                return
            when_ms = elapsed_ms(when_start)
            if not should_run:
                await _mark_step_skipped_when_false(
                    saga,
                    step,
                    db_conn=db_conn,
                    trace_context=trace_context,
                    when_cel_ms=when_ms,
                )
                skipped_count += 1
                continue

        schedule_acc = EngineTimingAccumulator()
        if when_cel:
            schedule_acc.add_ms("when_cel_ms", when_ms)
        await _notify_when_skipped_summary(
            saga,
            skipped_count=skipped_count,
            db_conn=db_conn,
            trace_context=trace_context,
        )
        await trigger_step(
            saga,
            order,
            db_conn=db_conn,
            trace_context=trace_context,
            schedule_engine_add=schedule_acc.to_dict() if schedule_acc.to_dict() else None,
        )
        return

    await _notify_when_skipped_summary(
        saga,
        skipped_count=skipped_count,
        db_conn=db_conn,
        trace_context=trace_context,
    )
    await handle_saga_completion(saga, db_conn=db_conn, trace_context=trace_context)


async def _mark_unreachable_forward_steps_skipped(
    saga_trace_id: str, *, db_conn: BaseDBAsyncClient
) -> int:
    """Mark forward rows still ``PENDING`` as ``SKIPPED`` when the saga cannot run them.

    Avoids leaving later blueprint steps ``PENDING`` after saga ``FAILED`` at an early
    step, or ``COMPENSATED`` / ``COMPLETED`` without those steps ever starting.

    Returns:
        Number of rows updated.
    """
    ended = datetime.now(UTC)
    return await (
        SagaStepInstance.filter(
            saga_trace_id=saga_trace_id,
            compensates_span_id__isnull=True,
            status=StepStatus.PENDING,
        )
        .using_db(db_conn)
        .update(status=StepStatus.SKIPPED, end_time=ended)
    )


def _ingest_trace_context(event: SagaIngestEvent | StepFailedEvent) -> dict[str, Any]:
    trace_context = getattr(event, "trace_context", None)
    return dict(trace_context) if isinstance(trace_context, dict) else {}


def _ingest_event_type_wire(event: SagaIngestEvent) -> str:
    return str(event.event_type)


async def _notify_skipped_ingest(
    event: SagaIngestEvent,
    *,
    dedup_reason: str,
    conn: BaseDBAsyncClient,
    event_dedup_key: str | None = None,
) -> None:
    step_span_id = getattr(event, "step_span_id", None) or ""
    dedup_key = event_dedup_key or (
        f"{event.saga_trace_id}:{_ingest_event_type_wire(event)}:{step_span_id}"
    )
    await get_registry().engine.on_ingest_deduplicated(
        namespace=event.namespace,
        saga_trace_id=event.saga_trace_id,
        step_span_id=step_span_id,
        reason=dedup_reason,
        conn=conn,
        trace_context=_ingest_trace_context(event),
        ingest_event_type=_ingest_event_type_wire(event),
        event_dedup_key=dedup_key,
        dedup_reason=dedup_reason,
    )


async def _notify_unreachable_steps_skipped(
    saga: SagaInstance,
    *,
    reason: str,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None = None,
) -> int:
    skipped_count = await _mark_unreachable_forward_steps_skipped(saga.trace_id, db_conn=db_conn)
    if skipped_count > 0:
        await get_registry().engine.on_steps_skipped_summary(
            namespace=saga.namespace,
            saga_trace_id=saga.trace_id,
            skipped_count=skipped_count,
            conn=db_conn,
            trace_context=trace_context,
            saga=saga,
            reason=reason,
        )
    return skipped_count


logger = logging.getLogger(__name__)

# SagaIngestEvent is an Annotated Union; validate with TypeAdapter.
_saga_ingest_adapter = TypeAdapter(SagaIngestEvent)

POLICY_PHASE_AFTER_REASON = "after_reason"
POLICY_PHASE_BEFORE_COMMIT = "before_commit"


def _event_payload_json_for_log(event: SagaIngestEvent) -> str:
    """Serialize ingest event for logs via Pydantic (avoids dict round-trip + json.dumps)."""
    if get_settings().log_pretty_json:
        return event.model_dump_json(indent=2)
    return event.model_dump_json()


def _mark_ingest_skipped_span(*, dedup_reason: str) -> None:
    """Record dedup/skip on the active consumer span (``process_saga_event`` / ``trace_boundary``)."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("ingest.skipped", True)
        span.set_attribute("ingest.skip_reason", dedup_reason)


_TIMEOUT_ERROR_CODES = frozenset({"TIMEOUT", "TIMEOUT_ERROR", "DEADLINE_EXCEEDED", "408"})


def _error_code_indicates_timeout(code: Any) -> bool:
    if code is None:
        return False
    code_upper = str(code).strip().upper()
    return code_upper in _TIMEOUT_ERROR_CODES or "TIMEOUT" in code_upper or "DEADLINE" in code_upper


def _error_text_indicates_timeout(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    lower = text.lower()
    return "timeout" in lower or "deadline" in lower


def _payload_indicates_timeout(raw_payload: dict[str, Any] | Any) -> bool:
    """Return True when structured error fields indicate a timeout (not full-payload scan)."""
    if not isinstance(raw_payload, dict):
        return False
    if _error_code_indicates_timeout(raw_payload.get("code")):
        return True
    reason = raw_payload.get("reason")
    if isinstance(reason, str) and reason.strip().lower() == "execution_timeout":
        return True
    return _error_text_indicates_timeout(
        raw_payload.get("message")
    ) or _error_text_indicates_timeout(raw_payload.get("error"))


def _first_tool_name(tool_specs: list[dict[str, Any]] | None) -> str:
    if not tool_specs:
        return ""
    first = tool_specs[0]
    if not isinstance(first, dict):
        return ""
    name = first.get("name")
    return str(name) if name else ""


def _policy_binding(
    *,
    phase: str,
    saga: SagaInstance,
    step: SagaStepInstance,
    arguments: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    tool_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ctx = coerce_dict(saga.context)
    return {
        "phase": phase,
        "input": coerce_dict(ctx.get("input")),
        "arguments": arguments or {},
        "output": output or {},
        "saga": {
            "trace_id": saga.trace_id,
            "namespace": saga.namespace,
            "status": status_value(saga.status),
        },
        "step": {
            "id": step.step_id,
            "name": step.step_name,
            "kind": step.step_kind,
            "order_index": step.order_index,
        },
        "worker": {"name": step.worker, "version": step.worker_version},
        "tool": {"name": _first_tool_name(tool_specs)},
    }


@trace_boundary(span_name_key="event_type")
async def process_saga_event(event_data: dict) -> None:
    """Core saga orchestration: validate event, run state machine in a transaction.

    Flow: SAGA_STARTED → DO_STEP or DO_COMMIT per step → STEP_COMPLETED → next
    worker command or SAGA_COMPLETED. Commit steps may run policy ``cel`` on resolved
    ``arguments`` before ``DO_COMMIT`` is queued. STEP_FAILED triggers compensation.

    Args:
        event_data: Ingest event dict (event_type, saga_trace_id, namespace,
            step_span_id for step-level events, trace_context, etc.).

    Raises:
        ValidationError: If event_data does not match SagaIngestEvent union.
        UnrecoverableError: If saga not found for trace_id/namespace.
    """
    try:
        event = _saga_ingest_adapter.validate_python(coerce_saga_ingest_dict(event_data))
    except ValidationError as e:
        logger.exception("Invalid event received: %s", e)
        raise

    async with in_transaction() as conn:
        try:
            step_span_id = getattr(event, "step_span_id", None) or ""
            event_dedup_key = f"{event.saga_trace_id}:{event.event_type}:{step_span_id}"
            try:
                await ProcessedIngestEvent.create(
                    event_dedup_key=event_dedup_key,
                    using_db=conn,
                )
            except IntegrityError:
                logger.debug(
                    "Ingest dedup: skipping already-processed event %s",
                    event_dedup_key,
                )
                await _notify_skipped_ingest(
                    event,
                    dedup_reason="processed_ingest_claim",
                    conn=conn,
                    event_dedup_key=event_dedup_key,
                )
                _mark_ingest_skipped_span(dedup_reason="processed_ingest_claim")
                return

            logger.info("----------------------------------------------------")
            logger.info("Handling Event: %s", event.event_type)
            logger.info("Saga Instance ID: %s", event.saga_trace_id)
            logger.info("Initial Payload: %s", _event_payload_json_for_log(event))
            logger.info("----------------------------------------------------")

            saga = (
                await SagaInstance.filter(trace_id=event.saga_trace_id, namespace=event.namespace)
                .using_db(conn)
                .select_for_update()
                .prefetch_related(
                    Prefetch(
                        "steps",
                        queryset=SagaStepInstance.filter(
                            saga_trace_id=event.saga_trace_id,
                            compensates_span_id__isnull=True,
                        ).order_by("order_index"),
                    )
                )
                .first()
            )

            if not saga:
                raise UnrecoverableError(
                    f"Saga {event.saga_trace_id} not found for tenant {event.namespace}"
                )

            match event.event_type:
                case EventType.SAGA_STARTED:
                    await handle_saga_started(saga, event, db_conn=conn)
                case EventType.STEP_COMPLETED:
                    await handle_step_completed(saga, event, db_conn=conn)
                case EventType.HUMAN_APPROVED:
                    await handle_hitl_approved(saga, event, db_conn=conn)
                case EventType.HUMAN_REJECTED:
                    await handle_human_rejected(saga, event, db_conn=conn)
                case EventType.HUMAN_RETRY:
                    await handle_human_retry(saga, event, db_conn=conn)
                case EventType.STEP_FAILED:
                    await handle_step_failed(saga, event, db_conn=conn)
                case EventType.STEP_COMPENSATED:
                    await handle_compensation_completed(saga, event, db_conn=conn)
                case EventType.COMPENSATION_FAILED:
                    await handle_compensation_failed(saga, event, db_conn=conn)
                case EventType.SAGA_COMPLETED:
                    await handle_saga_completion(saga, db_conn=conn)
                case EventType.SAGA_COMPENSATED:
                    pass  # Terminal notification; saga already COMPENSATED in DB
                case EventType.SAGA_FAILED:
                    pass  # Already handled when we emitted; idempotent no-op on re-ingest
                case _:
                    logger.warning("Unknown event_type: %s", event.event_type)

        except DoesNotExist:
            logger.error("Saga %s DoesNotExist.", event.saga_trace_id)
            raise UnrecoverableError(f"Saga {event.saga_trace_id} not found.") from None


# --- HITL lifecycle helpers
async def _enter_hitl_hold(
    saga: SagaInstance,
    step: SagaStepInstance,
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    review_subject: Literal["output", "arguments"],
    pending_payload: dict[str, Any],
) -> None:
    """Pause forward execution for human review; emit FSM and HITL hooks."""
    step_from = status_value(step.status)
    saga_from = status_value(saga.status)
    await get_registry().engine.on_hitl_review_requested(
        saga=saga,
        step=step,
        conn=db_conn,
        trace_context=trace_context,
        review_subject=review_subject,
        pending_payload=pending_payload,
    )
    step.status = StepStatus.AWAITING_HUMAN
    step.pending_review_payload = pending_payload
    step.hitl_review_started_at = datetime.now(UTC)
    await step.save(using_db=db_conn)
    saga.status = SagaStatus.AWAITING_HUMAN
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_step_transition(
        saga=saga,
        step=step,
        from_status=step_from,
        to_status=status_value(StepStatus.AWAITING_HUMAN),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.STEP_AWAITING_HUMAN,
        reason="hitl_review_requested",
    )
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=saga_from,
        to_status=status_value(SagaStatus.AWAITING_HUMAN),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.SAGA_AWAITING_HUMAN,
        reason="hitl_review_requested",
    )


async def _resume_saga_running_from_hitl(
    saga: SagaInstance,
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    reason: str,
) -> None:
    """Move saga from AWAITING_HUMAN to RUNNING with an explicit audit transition."""
    if saga.status != SagaStatus.AWAITING_HUMAN:
        return
    saga_from = status_value(saga.status)
    saga.status = SagaStatus.RUNNING
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=saga_from,
        to_status=status_value(SagaStatus.RUNNING),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.SAGA_RESUMED_FROM_HITL,
        reason=reason,
    )


def _rotate_step_idempotency_key_for_retry(step: SagaStepInstance, saga_trace_id: str) -> str:
    prior = step.idempotency_key
    step.idempotency_key = f"{saga_trace_id}-{step.step_id}-retry-{uuid.uuid4().hex[:8]}"
    return prior


async def _reset_step_for_worker_retry(
    saga: SagaInstance,
    step: SagaStepInstance,
    saga_trace_id: str,
    *,
    db_conn: BaseDBAsyncClient,
) -> str:
    """Clear draft step state, rotate idempotency key, drop ingest dedup. Returns prior key."""
    prior_key = step.idempotency_key
    _rotate_step_idempotency_key_for_retry(step, saga_trace_id)
    step.pending_review_payload = None
    step.output_payload = None
    step.error_details = None
    step.end_time = None
    clear_step_timing_fields(step)
    context = dict(saga.context) if saga.context else {"input": {}, "steps": {}}
    steps_map = context.get("steps")
    if isinstance(steps_map, dict) and step.step_id in steps_map:
        steps_map = dict(steps_map)
        del steps_map[step.step_id]
        context["steps"] = steps_map
        saga.context = context
    await _clear_worker_result_ingest_dedup(
        saga_trace_id,
        step.span_id,
        db_conn=db_conn,
    )
    await step.save(using_db=db_conn)
    await saga.save(using_db=db_conn)
    return prior_key


async def _reset_step_for_hitl_retry(
    step: SagaStepInstance,
    saga_trace_id: str,
    *,
    db_conn: BaseDBAsyncClient,
) -> str:
    """Clear held-step draft state and rotate worker idempotency key. Returns prior key."""
    prior_key = step.idempotency_key
    _rotate_step_idempotency_key_for_retry(step, saga_trace_id)
    step.pending_review_payload = None
    step.output_payload = None
    step.error_details = None
    step.end_time = None
    step.hitl_review_started_at = None
    clear_step_timing_fields(step)
    await step.save(using_db=db_conn)
    return prior_key


async def _clear_worker_result_ingest_dedup(
    saga_trace_id: str,
    step_span_id: str,
    *,
    db_conn: BaseDBAsyncClient,
) -> None:
    """Drop prior worker-result dedup keys so HITL retry can ingest a new completion."""
    keys = [
        f"{saga_trace_id}:{EventType.STEP_COMPLETED.value}:{step_span_id}",
        f"{saga_trace_id}:{EventType.STEP_FAILED.value}:{step_span_id}",
    ]
    await ProcessedIngestEvent.filter(event_dedup_key__in=keys).using_db(db_conn).delete()


# --- STATE HANDLERS
@trace_step()
async def handle_saga_started(
    saga: SagaInstance, event: SagaStartedEvent, db_conn: BaseDBAsyncClient
) -> None:
    """On SAGA_STARTED: set saga RUNNING and trigger first step.

    Idempotent if saga is already RUNNING (duplicate event ignored).
    """
    if not saga:
        logger.error("Missing saga instance.")
        return

    if saga.status != SagaStatus.PENDING:
        logger.warning(
            "Saga %s is already active (Status: %s). Ignoring duplicate SAGA_STARTED event.",
            saga.trace_id,
            saga.status,
        )
        return

    saga.status = SagaStatus.RUNNING
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=status_value(SagaStatus.PENDING),
        to_status=status_value(SagaStatus.RUNNING),
        conn=db_conn,
        trace_context=_ingest_trace_context(event),
        event_type=AuditEngineEventType.SAGA_STARTED,
    )

    await _schedule_next_forward_step(
        saga,
        after_order=-1,
        db_conn=db_conn,
        trace_context=_ingest_trace_context(event),
    )


@trace_step()
async def _finalize_step_output_and_advance(
    saga: SagaInstance,
    step: SagaStepInstance,
    output: dict[str, Any],
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Mark step COMPLETED, merge output into saga context, trigger next step or saga completion."""
    normalized = step_output_for_saga_context(output)
    from_status = status_value(step.status)
    step.status = StepStatus.COMPLETED
    step.output_payload = normalized
    step.pending_review_payload = None
    step.end_time = datetime.now(UTC)
    await step.save(using_db=db_conn)
    await get_registry().engine.on_step_transition(
        saga=saga,
        step=step,
        from_status=from_status,
        to_status=status_value(StepStatus.COMPLETED),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.STEP_COMPLETED,
        output_hash=hash_canonical_dict(normalized),
    )

    current_context = dict(saga.context) if saga.context else {"input": {}, "steps": {}}
    if "steps" not in current_context:
        current_context["steps"] = {}
    current_context["steps"][step.step_id] = step_context_entry_for_saga(output)
    saga.context = current_context
    await _resume_saga_running_from_hitl(
        saga,
        db_conn=db_conn,
        trace_context=trace_context,
        reason="step_completed_after_hitl",
    )
    await saga.save(using_db=db_conn)

    await _schedule_next_forward_step(
        saga,
        after_order=step.order_index,
        db_conn=db_conn,
        trace_context=trace_context,
    )


@trace_step()
async def handle_step_completed(
    saga: SagaInstance,
    event: StepCompletedIngestEvent,
    db_conn: BaseDBAsyncClient,
) -> None:
    """Worker finished the agentic step: validate output_schema if present, mark COMPLETED, merge context, trigger next or complete.

    Args:
        saga: Locked saga instance.
        event: STEP_COMPLETED payload (step_span_id, output).
        db_conn: Transaction connection.

    Returns:
        None. Late events for non-IN_PROGRESS step are ignored.
    """
    if not saga:
        logger.error("Missing saga.")
        return

    step = (
        await SagaStepInstance.filter(
            span_id=event.step_span_id,
            saga_trace_id=saga.trace_id,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )

    if not step:
        logger.error("Step %s not found in DB.", event.step_span_id)
        return

    if step.status != StepStatus.IN_PROGRESS:
        logger.warning(
            "Ignored late STEP_COMPLETED for Step %s. Current status: %s.",
            event.step_span_id,
            step.status,
        )
        await _notify_skipped_ingest(
            event,
            dedup_reason="late_step_state",
            conn=db_conn,
        )
        return

    await merge_step_timing_if_needed(step, worker_timing=event.timing, conn=db_conn)
    await merge_step_usage_if_needed(step, worker_usage=event.usage, conn=db_conn)

    if step.output_schema:
        try:
            validate_business_data_schema(
                event.output,
                step.output_schema,
                f"Step {step.step_id} output",
            )
        except Exception as e:
            logger.exception(
                "Step %s output failed schema validation: %s",
                event.step_span_id,
                e,
            )
            synthetic = StepFailedEvent(
                saga_trace_id=event.saga_trace_id,
                namespace=event.namespace,
                event_type=EventType.STEP_FAILED.value,
                step_span_id=event.step_span_id,
                error_details={"code": "OUTPUT_SCHEMA_VALIDATION_FAILED", "message": str(e)},
                output=event.output,
                timing=event.timing,
                usage=event.usage,
            )
            await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
            return

    trace_ctx = _ingest_trace_context(event)
    if step.step_kind == "reason" and step.policy_name and str(step.policy_name).strip():
        policy_start = time.perf_counter()
        gate = await run_policy_gate(
            policy_name=str(step.policy_name),
            phase=POLICY_PHASE_AFTER_REASON,
            binding=_policy_binding(
                phase=POLICY_PHASE_AFTER_REASON,
                saga=saga,
                step=step,
                arguments=step.resolved_arguments or {},
                output=event.output,
            ),
            denial_code="POLICY_REASON_DENIED",
            namespace=event.namespace,
            saga_trace_id=saga.trace_id,
            step_span_id=step.span_id,
            conn=db_conn,
            trace_context=trace_ctx,
        )
        add_engine_bucket_ms(step, bucket="policy_ms", ms=elapsed_ms(policy_start))
        if gate.outcome == PolicyGateOutcome.ERRORED:
            logger.error(
                "Reason step %s policy evaluation failed: %s",
                event.step_span_id,
                gate.error_message,
            )
            step.error_details = {
                "code": gate.error_code or "POLICY_EVALUATION_FAILED",
                "message": gate.error_message or "policy evaluation failed",
            }
            synthetic = StepFailedEvent(
                saga_trace_id=event.saga_trace_id,
                namespace=event.namespace,
                event_type=EventType.STEP_FAILED.value,
                step_span_id=event.step_span_id,
                error_details=step.error_details,
                output=event.output,
                timing=event.timing,
                usage=event.usage,
            )
            await step.save(
                using_db=db_conn,
                update_fields=["execution_timing", "execution_usage", "error_details"],
            )
            await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
            return

        if gate.outcome == PolicyGateOutcome.DENIED:
            step.error_details = {
                "code": "POLICY_REASON_DENIED",
                "message": "policy cel returned false; reason output not allowed",
            }
            synthetic = StepFailedEvent(
                saga_trace_id=event.saga_trace_id,
                namespace=event.namespace,
                event_type=EventType.STEP_FAILED.value,
                step_span_id=event.step_span_id,
                error_details=step.error_details,
                output=event.output,
                timing=event.timing,
                usage=event.usage,
            )
            await step.save(
                using_db=db_conn,
                update_fields=["execution_timing", "execution_usage", "error_details"],
            )
            await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
            return

    if step.hitl_required and step.step_kind == "reason":
        output_for_review = event.output if isinstance(event.output, dict) else {}
        await step.save(
            using_db=db_conn,
            update_fields=["execution_timing", "execution_usage"],
        )
        await _enter_hitl_hold(
            saga,
            step,
            db_conn=db_conn,
            trace_context=trace_ctx,
            review_subject="output",
            pending_payload=output_for_review,
        )
        logger.info(
            "Reason step %s held for HITL review (trace_id=%s)",
            event.step_span_id,
            saga.trace_id,
        )
        return

    await _finalize_step_output_and_advance(
        saga,
        step,
        event.output,
        db_conn=db_conn,
        trace_context=trace_ctx,
    )


@trace_step()
async def handle_hitl_approved(
    saga: SagaInstance,
    event: HumanApprovedIngestEvent,
    db_conn: BaseDBAsyncClient,
) -> None:
    """Resume a HITL-held reason or commit step after approval."""
    step = (
        await SagaStepInstance.filter(
            span_id=event.step_span_id,
            saga_trace_id=saga.trace_id,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )

    if not step:
        logger.error("Step %s not found in DB.", event.step_span_id)
        return

    if step.status == StepStatus.COMPLETED:
        logger.debug("Duplicate HUMAN_APPROVED for completed step %s ignored.", event.step_span_id)
        return

    if step.status != StepStatus.AWAITING_HUMAN:
        logger.warning(
            "HUMAN_APPROVED ignored for step %s (status=%s).",
            event.step_span_id,
            step.status,
        )
        return

    trace_ctx = _ingest_trace_context(event)

    if step.step_kind == "commit":
        tool_specs = step.tools_allow or []
        resource_specs = step.resources_allow or []
        if not isinstance(resource_specs, list):
            resource_specs = []
        if not isinstance(tool_specs, list) or len(tool_specs) != 1:
            synthetic = StepFailedEvent(
                saga_trace_id=event.saga_trace_id,
                namespace=event.namespace,
                event_type=EventType.STEP_FAILED.value,
                step_span_id=event.step_span_id,
                error_details={
                    "code": "VALIDATION_ERROR",
                    "message": "Commit step must have exactly one tool in tools_allow",
                },
            )
            await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
            return

        commit_args = step.resolved_arguments or {}
        if not isinstance(commit_args, dict):
            commit_args = {}
        output_override = event.output is not None
        await get_registry().engine.on_hitl_approved(
            saga=saga,
            step=step,
            conn=db_conn,
            trace_context=trace_ctx,
            namespace=event.namespace,
            saga_trace_id=saga.trace_id,
            step_span_id=step.span_id,
            step_kind="commit",
            output_override=output_override,
            effective_output=commit_args,
            pending_output=commit_args if output_override else None,
        )

        command = DoCommitCommand(
            type=CommandType.DO_COMMIT,
            namespace=saga.namespace,
            saga_trace_id=saga.trace_id,
            step_span_id=step.span_id,
            worker_name=step.worker,
            worker_version=step.worker_version,
            idempotency_key=step.idempotency_key,
            arguments=commit_args,
            tool_specs=slim_tool_specs(tool_specs),
            resource_specs=resource_specs,
        )
        step_from = status_value(step.status)
        await _resume_saga_running_from_hitl(
            saga,
            db_conn=db_conn,
            trace_context=trace_ctx,
            reason="hitl_approved_commit",
        )
        await get_registry().engine.on_step_transition(
            saga=saga,
            step=step,
            from_status=step_from,
            to_status=status_value(StepStatus.IN_PROGRESS),
            conn=db_conn,
            trace_context=trace_ctx,
            event_type=AuditEngineEventType.STEP_RESUMED_FROM_HITL,
            reason="hitl_approved_commit",
        )
        step.status = StepStatus.IN_PROGRESS
        step.pending_review_payload = None
        await step.save(using_db=db_conn)
        await emit_saga_event(
            topic=TOPIC_WORKER_COMMANDS,
            event_type=CommandType.DO_COMMIT.value,
            payload_schema=command,
            conn=db_conn,
        )
        await get_registry().engine.on_step_scheduled(
            saga=saga,
            step=step,
            conn=db_conn,
            trace_context=trace_ctx,
        )
        await get_registry().engine.on_step_started(
            saga=saga,
            step=step,
            conn=db_conn,
            trace_context=trace_ctx,
            from_status=StepStatus.AWAITING_HUMAN,
        )
        logger.info("Queued DO_COMMIT for HITL-approved step %s", step.span_id)
        return

    output = event.output if event.output is not None else step.pending_review_payload
    if output is None:
        logger.error("HUMAN_APPROVED: no output for step %s", event.step_span_id)
        return

    if step.output_schema:
        try:
            validate_business_data_schema(
                output,
                step.output_schema,
                f"Step {step.step_id} output",
            )
        except Exception as e:
            logger.exception(
                "Step %s output failed schema validation on approve: %s",
                event.step_span_id,
                e,
            )
            synthetic = StepFailedEvent(
                saga_trace_id=event.saga_trace_id,
                namespace=event.namespace,
                event_type=EventType.STEP_FAILED.value,
                step_span_id=event.step_span_id,
                error_details={"code": "OUTPUT_SCHEMA_VALIDATION_FAILED", "message": str(e)},
                output=output,
            )
            await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
            return

    output_override = event.output is not None
    pending_output = (
        step.pending_review_payload if isinstance(step.pending_review_payload, dict) else {}
    )
    effective_output = output if isinstance(output, dict) else {}
    await get_registry().engine.on_hitl_approved(
        saga=saga,
        step=step,
        conn=db_conn,
        trace_context=trace_ctx,
        namespace=event.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        step_kind="reason",
        output_override=output_override,
        effective_output=effective_output,
        pending_output=pending_output if output_override else None,
    )

    await _finalize_step_output_and_advance(
        saga,
        step,
        output,
        db_conn=db_conn,
        trace_context=trace_ctx,
    )


@trace_step()
async def handle_human_rejected(
    saga: SagaInstance,
    event: HumanRejectedIngestEvent,
    db_conn: BaseDBAsyncClient,
) -> None:
    """Treat HITL rejection as a clean step failure."""
    step = (
        await SagaStepInstance.filter(
            span_id=event.step_span_id,
            saga_trace_id=saga.trace_id,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )

    if not step:
        logger.error("Step %s not found in DB.", event.step_span_id)
        return

    if step.status != StepStatus.AWAITING_HUMAN:
        logger.warning(
            "HUMAN_REJECTED ignored for step %s (status=%s).",
            event.step_span_id,
            step.status,
        )
        return

    details = event.error_details or {"code": "HUMAN_REJECTED"}
    if not isinstance(details, dict):
        details = {"code": "HUMAN_REJECTED"}
    error_code = str(details.get("code") or "HUMAN_REJECTED")
    rejection_message = details.get("message")
    rejection_reason = str(rejection_message) if rejection_message is not None else None
    await get_registry().engine.on_hitl_rejected(
        saga=saga,
        step=step,
        conn=db_conn,
        trace_context=_ingest_trace_context(event),
        namespace=event.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        error_code=error_code,
        rejection_reason=rejection_reason,
    )
    synthetic = StepFailedEvent(
        saga_trace_id=event.saga_trace_id,
        namespace=event.namespace,
        event_type=EventType.STEP_FAILED.value,
        step_span_id=event.step_span_id,
        error_details=details,
        output=step.pending_review_payload,
    )
    await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)


@trace_step()
async def handle_human_retry(
    saga: SagaInstance,
    event: HumanRetryIngestEvent,
    db_conn: BaseDBAsyncClient,
) -> None:
    """Re-queue worker execution for a HITL-held step; preserve upstream saga context."""
    step = (
        await SagaStepInstance.filter(
            span_id=event.step_span_id,
            saga_trace_id=saga.trace_id,
            compensates_span_id__isnull=True,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )
    if not step:
        logger.error("Step %s not found in DB.", event.step_span_id)
        return

    if step.status != StepStatus.AWAITING_HUMAN:
        logger.warning(
            "HUMAN_RETRY ignored for step %s (status=%s).",
            event.step_span_id,
            step.status,
        )
        return

    if saga.status not in (SagaStatus.AWAITING_HUMAN, SagaStatus.RUNNING):
        logger.warning(
            "HUMAN_RETRY ignored for saga %s (status=%s).",
            saga.trace_id,
            saga.status,
        )
        return

    try:
        assert_hitl_retry_allowed(step)
    except HitlRetryLimitError as e:
        logger.warning(
            "HUMAN_RETRY limit reached for step %s (%s).",
            event.step_span_id,
            e,
        )
        return

    trace_ctx = _ingest_trace_context(event)
    guidance = getattr(event, "retry_guidance", None)
    if isinstance(guidance, str):
        guidance = guidance.strip() or None
    else:
        guidance = None

    prior_worker_key = await _reset_step_for_hitl_retry(step, saga.trace_id, db_conn=db_conn)
    await release_worker_claim_for_retry(prior_worker_key, conn=db_conn)
    await _clear_worker_result_ingest_dedup(
        saga.trace_id,
        step.span_id,
        db_conn=db_conn,
    )

    step.hitl_retry_count = int(step.hitl_retry_count) + 1
    await step.save(using_db=db_conn)

    new_worker_key = step.idempotency_key
    await get_registry().engine.on_hitl_retry_requested(
        saga=saga,
        step=step,
        conn=db_conn,
        trace_context=trace_ctx,
        prior_idempotency_key=prior_worker_key,
        new_idempotency_key=new_worker_key,
        retry_guidance=guidance,
        attempt=int(step.hitl_retry_count),
        max_retries=step.hitl_max_retries,
    )

    step_from = status_value(StepStatus.AWAITING_HUMAN)
    await _resume_saga_running_from_hitl(
        saga,
        db_conn=db_conn,
        trace_context=trace_ctx,
        reason="human_retry",
    )
    await get_registry().engine.on_step_transition(
        saga=saga,
        step=step,
        from_status=step_from,
        to_status=status_value(StepStatus.IN_PROGRESS),
        conn=db_conn,
        trace_context=trace_ctx,
        event_type=AuditEngineEventType.STEP_RESUMED_FROM_HITL,
        reason="human_retry",
    )

    await trigger_step(
        saga,
        step.order_index,
        db_conn=db_conn,
        trace_context=trace_ctx,
        step_start_from_status=StepStatus.AWAITING_HUMAN,
        allow_from_awaiting_human=True,
        hitl_retry_guidance=guidance,
    )


def _step_failure_payload(event: StepFailedEvent) -> dict[str, Any]:
    """Normalize failure details from a STEP_FAILED-shaped event."""
    if isinstance(event.error_details, dict):
        return event.error_details
    if isinstance(event.output, dict):
        return event.output
    return {}


async def _apply_step_failure_lifecycle(
    saga: SagaInstance,
    step: SagaStepInstance,
    event: StepFailedEvent,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Apply step failure status, audit hooks, and compensation using a locked step row.

    Caller must already hold ``step`` under ``select_for_update`` in ``db_conn``.
    """
    raw_payload = _step_failure_payload(event)
    trace_ctx = trace_context if trace_context is not None else _ingest_trace_context(event)
    await merge_step_timing_if_needed(step, worker_timing=event.timing, conn=db_conn)
    await merge_step_usage_if_needed(step, worker_usage=event.usage, conn=db_conn)
    reaper_pre_timed_out = step.status == StepStatus.TIMED_OUT

    logger.warning("Saga %s failed at step %s.", saga.trace_id, step.order_index)
    from_status = StepStatus.IN_PROGRESS if reaper_pre_timed_out else status_value(step.status)

    is_payload_timeout = _payload_indicates_timeout(raw_payload)
    if not reaper_pre_timed_out:
        if is_payload_timeout:
            step.status = StepStatus.TIMED_OUT
        else:
            step.status = StepStatus.FAILED

    if not step.error_details:
        step.error_details = raw_payload
    if step.end_time is None:
        step.end_time = datetime.now(UTC)
    await step.save(
        using_db=db_conn,
        update_fields=[
            "status",
            "error_details",
            "end_time",
            "execution_timing",
            "pending_engine_timing",
            "execution_usage",
        ],
    )
    code = raw_payload.get("code")
    step_event_type = (
        AuditEngineEventType.STEP_TIMED_OUT
        if step.status == StepStatus.TIMED_OUT
        else AuditEngineEventType.STEP_FAILED
    )
    await get_registry().engine.on_step_transition(
        saga=saga,
        step=step,
        from_status=from_status,
        to_status=status_value(step.status),
        conn=db_conn,
        trace_context=trace_ctx,
        event_type=step_event_type,
        error_code=str(code) if code is not None else None,
    )

    is_dirty_failure = step.status == StepStatus.TIMED_OUT or code == "SYSTEM_CRASH"
    start_compensation_index = step.order_index if is_dirty_failure else step.order_index - 1

    if start_compensation_index < 0:
        logger.info("Saga %s failed cleanly at start. No compensation needed.", saga.trace_id)
        prior_saga_status = saga.status
        saga.status = SagaStatus.FAILED
        await saga.save(using_db=db_conn)
        await get_registry().engine.on_saga_transition(
            saga=saga,
            from_status=status_value(prior_saga_status),
            to_status=status_value(SagaStatus.FAILED),
            conn=db_conn,
            trace_context=trace_ctx,
            event_type=AuditEngineEventType.SAGA_FAILED,
            reason="step_failed_at_start",
        )
        await _notify_unreachable_steps_skipped(
            saga,
            reason="step_failed_at_start",
            db_conn=db_conn,
            trace_context=trace_ctx,
        )
        failed_payload = SagaEventPayload(
            namespace=saga.namespace,
            saga_trace_id=saga.trace_id,
            step_span_id=None,
            status="SAGA_FAILED",
            output={
                "reason": "step_failed_at_start",
                "step_span_id": event.step_span_id,
                "step_order": step.order_index,
                "error_details": raw_payload,
                "failed_at": str(datetime.now(UTC)),
            },
        )
        await emit_saga_event(
            topic=TOPIC_ORCHESTRATOR_EVENTS,
            event_type=EventType.SAGA_FAILED.value,
            payload_schema=failed_payload,
            conn=db_conn,
        )
        return

    logger.info("Triggering compensation starting at index %s", start_compensation_index)
    prior_saga_status = saga.status
    saga.status = SagaStatus.COMPENSATING
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=status_value(prior_saga_status),
        to_status=status_value(SagaStatus.COMPENSATING),
        conn=db_conn,
        trace_context=trace_ctx,
        event_type=AuditEngineEventType.SAGA_COMPENSATING,
    )
    await trigger_compensation(
        saga,
        step_order=start_compensation_index,
        db_conn=db_conn,
        trace_context=trace_ctx,
    )


@trace_step()
async def handle_step_failed(
    saga: SagaInstance, event: StepFailedEvent, db_conn: BaseDBAsyncClient
):
    """Handle STEP_FAILED ingest: lock step, dedupe, then run failure lifecycle.

    Sets step status (FAILED or TIMED_OUT from payload), then triggers
    compensation from current step (dirty/timeout) or previous (clean failure).
    If step order is 0, marks saga FAILED and emits SAGA_FAILED.

    Args:
        saga: Locked saga instance.
        event: Step failure event (step_span_id, error_details, output).
        db_conn: Transaction connection.

    Returns:
        None. Duplicate events for already terminal step are ignored.
    """
    if not saga:
        logger.error("Missing saga.")
        return

    step = (
        await SagaStepInstance.filter(span_id=event.step_span_id, saga_trace_id=saga.trace_id)
        .using_db(db_conn)
        .select_for_update()
        .first()
    )

    if not step:
        logger.error("Step %s not found in DB.", event.step_span_id)
        return

    if step.status in (StepStatus.COMPENSATING, StepStatus.COMPENSATED, StepStatus.FAILED):
        logger.info("Duplicate event ignored for step %s (terminal)", step.span_id)
        await _notify_skipped_ingest(
            event,
            dedup_reason="terminal_step_state",
            conn=db_conn,
        )
        return

    if step.status == StepStatus.TIMED_OUT and saga.status in (
        SagaStatus.COMPENSATING,
        SagaStatus.COMPENSATED,
        SagaStatus.FAILED,
    ):
        logger.info("Duplicate timeout ingest ignored for step %s", step.span_id)
        await _notify_skipped_ingest(
            event,
            dedup_reason="duplicate_timeout_ingest",
            conn=db_conn,
        )
        return

    await _apply_step_failure_lifecycle(saga, step, event, db_conn)


@trace_step()
async def handle_compensation_completed(
    saga: SagaInstance, event: StepCompensatedIngestEvent, db_conn: BaseDBAsyncClient
):
    """Compensation step finished; trigger previous step's compensation or mark saga COMPENSATED.

    ``STEP_COMPENSATED`` must reference the **compensation execution** row (``compensates_span_id``
    set, status ``COMPENSATING``).

    Args:
        saga: Locked saga instance.
        event: STEP_COMPENSATED payload (step_span_id, output).
        db_conn: Transaction connection.

    Returns:
        None. Already-compensated step is ignored (idempotent).
    """
    if not saga:
        logger.error("Missing saga.")
        return

    step = (
        await SagaStepInstance.filter(span_id=event.step_span_id, saga_trace_id=saga.trace_id)
        .using_db(db_conn)
        .select_for_update()
        .first()
    )

    if not step:
        logger.error("Step %s not found.", event.step_span_id)
        return

    if step.status == StepStatus.COMPENSATED:
        logger.warning("Step %s is already compensated. Ignoring.", event.step_span_id)
        await _notify_skipped_ingest(
            event,
            dedup_reason="already_compensated",
            conn=db_conn,
        )
        return

    if step.compensates_span_id is None:
        logger.warning(
            "STEP_COMPENSATED ignored for forward row %s (expected compensation row).",
            event.step_span_id,
        )
        return

    if step.status != StepStatus.COMPENSATING:
        logger.warning(
            "STEP_COMPENSATED ignored for compensation row %s (status=%s).",
            event.step_span_id,
            step.status,
        )
        return

    trace_ctx = _ingest_trace_context(event)
    await finalize_step_execution_timing(step, worker_timing=event.timing, conn=db_conn)
    await finalize_step_execution_usage(step, worker_usage=event.usage, conn=db_conn)
    from_status = status_value(step.status)
    step.status = StepStatus.COMPENSATED
    step.end_time = datetime.now(UTC)
    step.output_payload = event.output
    await step.save(
        using_db=db_conn,
        update_fields=[
            "status",
            "end_time",
            "output_payload",
            "execution_timing",
            "pending_engine_timing",
            "execution_usage",
        ],
    )
    output_hash = None
    if isinstance(event.output, dict):
        output_hash = hash_canonical_dict(event.output)
    await get_registry().engine.on_step_transition(
        saga=saga,
        step=step,
        from_status=from_status,
        to_status=status_value(StepStatus.COMPENSATED),
        conn=db_conn,
        trace_context=trace_ctx,
        event_type=AuditEngineEventType.STEP_COMPENSATED,
        output_hash=output_hash,
    )

    logger.info(
        "Step %s (%s) compensated successfully.",
        step.order_index,
        step.step_name,
    )

    await _advance_lifo_compensation(
        saga,
        step.order_index - 1,
        db_conn=db_conn,
        trace_context=trace_ctx,
    )


@trace_step()
async def handle_compensation_failed(
    saga: SagaInstance,
    event: CompensationFailedIngestEvent,
    db_conn: BaseDBAsyncClient,
) -> None:
    """Worker reported compensation step failed: mark step and saga FAILED, emit SAGA_FAILED.

    Args:
        saga: Locked saga instance.
        event: COMPENSATION_FAILED payload (step_span_id, error_details).
        db_conn: Transaction connection.

    Returns:
        None.
    """
    if not saga:
        logger.error("Missing saga.")
        return

    step = (
        await SagaStepInstance.filter(
            span_id=event.step_span_id,
            saga_trace_id=saga.trace_id,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )

    trace_ctx = _ingest_trace_context(event)
    if step:
        await finalize_step_execution_timing(step, worker_timing=event.timing, conn=db_conn)
        await finalize_step_execution_usage(step, worker_usage=event.usage, conn=db_conn)
        from_status = status_value(step.status)
        step.status = StepStatus.FAILED
        step.end_time = datetime.now(UTC)
        step.error_details = event.error_details or event.output
        await step.save(
            using_db=db_conn,
            update_fields=[
                "status",
                "end_time",
                "error_details",
                "execution_timing",
                "pending_engine_timing",
                "execution_usage",
            ],
        )
        await get_registry().engine.on_step_transition(
            saga=saga,
            step=step,
            from_status=from_status,
            to_status=status_value(StepStatus.FAILED),
            conn=db_conn,
            trace_context=trace_ctx,
            event_type=AuditEngineEventType.STEP_FAILED,
            reason="compensation_failed",
        )

    logger.warning(
        "Saga %s failed: compensation step %s failed.",
        saga.trace_id,
        event.step_span_id,
    )

    await _emit_saga_failed_from_compensation(
        saga,
        db_conn=db_conn,
        trace_context=trace_ctx,
        reason="compensation_failed",
        step_span_id=event.step_span_id,
        error_details=event.error_details or event.output,
    )


@trace_step()
async def handle_saga_completion(
    saga: SagaInstance,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Mark saga COMPLETED and emit SAGA_COMPLETED. Idempotent if already COMPLETED.

    Args:
        saga: Saga instance (not necessarily locked).
        db_conn: DB connection for save and emit.

    Returns:
        None.
    """
    if saga.status == SagaStatus.COMPLETED:
        logger.debug("Saga %s already COMPLETED; skipping.", saga.trace_id)
        return
    logger.info("Saga %s completed successfully.", saga.trace_id)
    prior_saga_status = saga.status
    saga.status = SagaStatus.COMPLETED
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=status_value(prior_saga_status),
        to_status=status_value(SagaStatus.COMPLETED),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.SAGA_COMPLETED,
    )
    await _notify_unreachable_steps_skipped(
        saga,
        reason="saga_completed",
        db_conn=db_conn,
        trace_context=trace_context,
    )
    completed_at = datetime.now(UTC)
    final_payload = SagaEventPayload(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=None,
        status="SAGA_COMPLETED",
        output={
            "completed_at": str(completed_at),
            "outcome": "Success",
        },
    )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type="SAGA_COMPLETED",
        payload_schema=final_payload,
        conn=db_conn,
    )


def _step_tool_and_resource_specs(step: SagaStepInstance) -> tuple[list[dict[str, Any]], list[Any]]:
    tool_specs = step.tools_allow or []
    if not isinstance(tool_specs, list):
        tool_specs = []
    resource_specs = step.resources_allow or []
    if not isinstance(resource_specs, list):
        resource_specs = []
    return tool_specs, resource_specs


async def _commit_policy_stopped_step(
    *,
    saga: SagaInstance,
    step: SagaStepInstance,
    worker_args: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    schedule_acc: EngineTimingAccumulator | None = None,
) -> bool:
    pn = (step.policy_name or "").strip()
    if not pn:
        return False
    policy_start = time.perf_counter()
    gate = await run_policy_gate(
        policy_name=pn,
        phase=POLICY_PHASE_BEFORE_COMMIT,
        binding=_policy_binding(
            phase=POLICY_PHASE_BEFORE_COMMIT,
            saga=saga,
            step=step,
            arguments=worker_args,
            tool_specs=tool_specs,
        ),
        denial_code="POLICY_COMMIT_DENIED",
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        conn=db_conn,
        trace_context=trace_context,
    )
    if schedule_acc is not None:
        schedule_acc.add_ms("policy_ms", elapsed_ms(policy_start))
    if gate.outcome == PolicyGateOutcome.ERRORED:
        if schedule_acc is not None:
            await persist_schedule_engine_timing_on_policy_denial(step, schedule_acc, conn=db_conn)
        synthetic = StepFailedEvent(
            saga_trace_id=saga.trace_id,
            namespace=saga.namespace,
            event_type=EventType.STEP_FAILED.value,
            step_span_id=step.span_id,
            error_details={
                "code": gate.error_code or "POLICY_EVALUATION_FAILED",
                "message": gate.error_message or "policy evaluation failed",
            },
        )
        await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
        return True
    if gate.outcome == PolicyGateOutcome.DENIED:
        if schedule_acc is not None:
            await persist_schedule_engine_timing_on_policy_denial(step, schedule_acc, conn=db_conn)
        synthetic = StepFailedEvent(
            saga_trace_id=saga.trace_id,
            namespace=saga.namespace,
            event_type=EventType.STEP_FAILED.value,
            step_span_id=step.span_id,
            error_details={
                "code": "POLICY_COMMIT_DENIED",
                "message": "policy cel returned false; commit not allowed",
            },
        )
        await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
        return True
    return False


async def _hold_commit_step_for_hitl(
    *,
    saga: SagaInstance,
    step: SagaStepInstance,
    worker_args: dict[str, Any],
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
) -> None:
    args_for_review = worker_args if isinstance(worker_args, dict) else {}
    await _enter_hitl_hold(
        saga,
        step,
        db_conn=db_conn,
        trace_context=trace_context,
        review_subject="arguments",
        pending_payload=args_for_review,
    )


async def _build_commit_worker_command(
    *,
    saga: SagaInstance,
    step: SagaStepInstance,
    worker_args: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    resource_specs: list[Any],
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    schedule_acc: EngineTimingAccumulator | None = None,
) -> tuple[DoCommitCommand, str] | None:
    if len(tool_specs) != 1:
        raise ValueError(
            f"Commit step {step.span_id} must have exactly one tool in tools_allow; "
            f"got {len(tool_specs)}"
        )
    if await _commit_policy_stopped_step(
        saga=saga,
        step=step,
        worker_args=worker_args,
        tool_specs=tool_specs,
        db_conn=db_conn,
        trace_context=trace_context,
        schedule_acc=schedule_acc,
    ):
        return None
    if step.hitl_required:
        await _hold_commit_step_for_hitl(
            saga=saga,
            step=step,
            worker_args=worker_args,
            db_conn=db_conn,
            trace_context=trace_context,
        )
        return None
    cmd = DoCommitCommand(
        type=CommandType.DO_COMMIT,
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        worker_name=step.worker,
        worker_version=step.worker_version,
        idempotency_key=step.idempotency_key,
        arguments=step.resolved_arguments or {},
        tool_specs=slim_tool_specs(tool_specs),
        resource_specs=resource_specs,
    )
    return cmd, CommandType.DO_COMMIT.value


async def _build_reason_worker_command(
    *,
    saga: SagaInstance,
    step: SagaStepInstance,
    tool_specs: list[dict[str, Any]],
    resource_specs: list[Any],
) -> tuple[DoStepCommand, str]:
    if not step.prompt_ref:
        raise ValueError(
            f"Step {step.span_id} has no prompt_ref; "
            "PROMPTS_ROOT and file-based prompts are required for reason steps."
        )
    prompts_root = get_settings().prompts_root
    if not prompts_root or not str(prompts_root).strip():
        raise ValueError(
            "prompts_root is not configured; set PROMPTS_ROOT when scheduling reason steps."
        )
    await asyncio.to_thread(assert_prompt_file_exists, prompts_root, step.prompt_ref)
    cmd = DoStepCommand(
        type=CommandType.DO_STEP,
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=step.span_id,
        worker_name=step.worker,
        worker_version=step.worker_version,
        idempotency_key=step.idempotency_key,
        prompt_ref=step.prompt_ref,
        arguments=step.resolved_arguments or {},
        tool_specs=slim_tool_specs(tool_specs),
        resource_specs=resource_specs,
    )
    return cmd, CommandType.DO_STEP.value


async def _build_forward_worker_command(
    *,
    saga: SagaInstance,
    step: SagaStepInstance,
    worker_args: dict[str, Any],
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    schedule_acc: EngineTimingAccumulator | None = None,
) -> tuple[DoStepCommand | DoCommitCommand, str] | None:
    tool_specs, resource_specs = _step_tool_and_resource_specs(step)
    if step.step_kind not in SAGA_STEP_KINDS:
        raise ValueError(
            f"Step {step.span_id} has invalid step_kind: {step.step_kind!r}; "
            f"expected one of {sorted(SAGA_STEP_KINDS)!r}"
        )
    if step.step_kind == "commit":
        return await _build_commit_worker_command(
            saga=saga,
            step=step,
            worker_args=worker_args,
            tool_specs=tool_specs,
            resource_specs=resource_specs,
            db_conn=db_conn,
            trace_context=trace_context,
            schedule_acc=schedule_acc,
        )
    return await _build_reason_worker_command(
        saga=saga,
        step=step,
        tool_specs=tool_specs,
        resource_specs=resource_specs,
    )


# --- ACTION TRIGGERS
@trace_step()
async def trigger_step(
    saga: SagaInstance,
    step_order: int,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
    step_start_from_status: StepStatus | str | None = None,
    allow_from_awaiting_human: bool = False,
    allow_retry_in_progress: bool = False,
    hitl_retry_guidance: str | None = None,
    schedule_engine_add: dict[str, int] | None = None,
) -> None:
    """Queue DO_STEP (reason) or DO_COMMIT (commit) for the step; set IN_PROGRESS.

    Caller must be inside a transaction (db_conn). Reason steps load prompt from
    PROMPTS_ROOT and validate template variables. Commit steps resolve parameters only.

    Args:
        saga: Saga instance (locked).
        step_order: Order index of the step to run.
        db_conn: Transaction connection.

    Returns:
        None. No-op if step missing, already IN_PROGRESS, or COMPLETED. On
        prompt/validation error step is marked FAILED and no command is sent.

    Raises:
        ValueError: Reason step with no prompt_ref, or PROMPTS_ROOT/prompt load fails.
    """
    step_to_run = (
        await SagaStepInstance.filter(
            saga_trace_id=saga.trace_id,
            order_index=step_order,
            compensates_span_id__isnull=True,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )

    if not step_to_run:
        logger.error(
            "Saga %s tried to trigger non-existing step order: %s",
            saga.trace_id,
            step_order,
        )
        return

    if step_to_run.status == StepStatus.COMPLETED:
        logger.warning(
            "Step %s is already %s. Ignoring trigger.",
            step_order,
            step_to_run.status,
        )
        return
    if step_to_run.status == StepStatus.IN_PROGRESS and not (
        allow_from_awaiting_human or allow_retry_in_progress
    ):
        logger.warning(
            "Step %s is already %s. Ignoring trigger.",
            step_order,
            step_to_run.status,
        )
        return
    if step_to_run.status == StepStatus.AWAITING_HUMAN and not allow_from_awaiting_human:
        logger.warning(
            "Step %s is AWAITING_HUMAN. Ignoring trigger (use HUMAN_RETRY).",
            step_order,
        )
        return

    schedule_from = (
        status_value(step_start_from_status)
        if step_start_from_status is not None
        else status_value(step_to_run.status)
    )
    step_to_run.status = StepStatus.IN_PROGRESS
    step_to_run.started_at = datetime.now(UTC)
    await step_to_run.save(using_db=db_conn)

    schedule_acc = EngineTimingAccumulator()
    if schedule_engine_add:
        for key, val in schedule_engine_add.items():
            schedule_acc.add_ms(key, int(val))
    schedule_acc.start("schedule")

    try:
        worker_args = resolve_parameters_spec(
            step_to_run.parameters_spec or {},
            saga.context or {},
        )
        if step_to_run.step_kind == "reason" and (
            int(step_to_run.hitl_retry_count) > 0 or hitl_retry_guidance
        ):
            worker_args = merge_hitl_retry_into_arguments(
                worker_args,
                step_to_run,
                guidance_override=hitl_retry_guidance,
                attempt=int(step_to_run.hitl_retry_count),
            )
        step_to_run.resolved_arguments = worker_args
        await step_to_run.save(using_db=db_conn)

        built = await _build_forward_worker_command(
            saga=saga,
            step=step_to_run,
            worker_args=worker_args,
            db_conn=db_conn,
            trace_context=trace_context,
            schedule_acc=schedule_acc,
        )
        schedule_acc.stop("schedule", bucket="schedule_ms")
        if built is None:
            if step_to_run.status == StepStatus.AWAITING_HUMAN and schedule_acc.to_dict():
                from common.execution_timing import merge_execution_timing

                step_to_run.execution_timing = merge_execution_timing(
                    engine=schedule_acc.to_dict(),
                    existing=step_to_run.execution_timing
                    if isinstance(step_to_run.execution_timing, dict)
                    else None,
                )
                await step_to_run.save(using_db=db_conn, update_fields=["execution_timing"])
            return
        command, event_type = built
        log_cmd = event_type
    except (ValidationError, ValueError) as e:
        logger.exception(
            "Failed to create worker command for step %s: %s",
            step_order,
            e,
        )
        synthetic = StepFailedEvent(
            saga_trace_id=saga.trace_id,
            namespace=saga.namespace,
            event_type=EventType.STEP_FAILED.value,
            step_span_id=step_to_run.span_id,
            error_details={"code": "VALIDATION_ERROR", "message": str(e)},
        )
        await _apply_step_failure_lifecycle(saga, step_to_run, synthetic, db_conn)
        return

    logger.info("Queuing %s command for step %s", log_cmd, step_order)
    await emit_saga_event(
        topic=TOPIC_WORKER_COMMANDS,
        event_type=event_type,
        payload_schema=command,
        conn=db_conn,
    )
    await persist_pending_engine_timing(
        step_to_run,
        engine_add=schedule_acc.to_dict(),
        dispatch_anchor=time.perf_counter(),
        conn=db_conn,
    )
    await get_registry().engine.on_step_scheduled(
        saga=saga,
        step=step_to_run,
        conn=db_conn,
        trace_context=trace_context,
    )
    await get_registry().engine.on_step_started(
        saga=saga,
        step=step_to_run,
        conn=db_conn,
        trace_context=trace_context,
        from_status=schedule_from,
    )


async def _finalize_saga_compensated(
    saga: SagaInstance,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
) -> None:
    logger.info("Saga %s reached STATE_RESTORED.", saga.trace_id)

    prior_saga_status = saga.status
    saga.status = SagaStatus.COMPENSATED
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=status_value(prior_saga_status),
        to_status=status_value(SagaStatus.COMPENSATED),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.SAGA_COMPENSATED,
    )
    await _notify_unreachable_steps_skipped(
        saga,
        reason="saga_compensated",
        db_conn=db_conn,
        trace_context=trace_context,
    )

    final_payload = SagaEventPayload(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=None,
        status="SAGA_COMPENSATED",
        output={
            "completed_at": str(datetime.now(UTC)),
            "outcome": "STATE_RESTORED",
        },
    )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type=EventType.SAGA_COMPENSATED.value,
        payload_schema=final_payload,
        conn=db_conn,
    )


async def _emit_saga_failed_from_compensation(
    saga: SagaInstance,
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    reason: str,
    step_span_id: str | None = None,
    error_details: dict[str, Any] | None = None,
) -> None:
    prior_saga_status = saga.status
    saga.status = SagaStatus.FAILED
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=status_value(prior_saga_status),
        to_status=status_value(SagaStatus.FAILED),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.SAGA_FAILED,
        reason=reason,
    )
    await _notify_unreachable_steps_skipped(
        saga,
        reason="saga_failed",
        db_conn=db_conn,
        trace_context=trace_context,
    )

    output: dict[str, Any] = {
        "reason": reason,
        "failed_at": str(datetime.now(UTC)),
    }
    if step_span_id is not None:
        output["step_span_id"] = step_span_id
    if error_details is not None:
        output["error_details"] = error_details

    failed_payload = SagaEventPayload(
        namespace=saga.namespace,
        saga_trace_id=saga.trace_id,
        step_span_id=None,
        status="SAGA_FAILED",
        output=output,
    )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type=EventType.SAGA_FAILED.value,
        payload_schema=failed_payload,
        conn=db_conn,
    )


async def _load_forward_step_for_compensation(
    saga: SagaInstance,
    order: int,
    db_conn: BaseDBAsyncClient,
) -> SagaStepInstance | None:
    return (
        await SagaStepInstance.filter(
            saga_trace_id=saga.trace_id,
            order_index=order,
            compensates_span_id__isnull=True,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )


async def _undo_row_status_exists(
    saga: SagaInstance,
    forward_span_id: str,
    undo_status: StepStatus,
    db_conn: BaseDBAsyncClient,
) -> bool:
    return (
        await SagaStepInstance.filter(
            saga_trace_id=saga.trace_id,
            compensates_span_id=forward_span_id,
            status=undo_status,
        )
        .using_db(db_conn)
        .exists()
    )


async def _schedule_compensation_for_forward(
    saga: SagaInstance,
    forward: SagaStepInstance,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
) -> None:
    new_span = uuid.uuid4().hex[:16]
    cmd_idempotency_key = f"comp-{saga.trace_id}-{new_span}"

    schedule_acc = EngineTimingAccumulator()
    schedule_acc.start("comp_schedule")

    comp_def = forward.compensation_definition or {}
    with_spec = comp_def.get("with") or {}
    resolve_ctx = compensation_parameter_context(
        saga,
        forward,
        undo_span_id=new_span,
        idempotency_key=cmd_idempotency_key,
    )
    resolved_comp_input = resolve_parameters_spec(with_spec, resolve_ctx)
    comp_worker, comp_worker_version = resolve_worker_from_compensation(
        comp_def,
        forward_worker=forward.worker,
        forward_worker_version=forward.worker_version,
    )
    comp_tool_specs, comp_resource_specs = compensation_tool_resource_specs(comp_def)
    comp_max_turns = comp_def.get("max_turns")
    undo_max_turns = int(comp_max_turns) if isinstance(comp_max_turns, int) else forward.max_turns

    worker_row = await WorkerDefinition.get_or_none(
        name=comp_worker,
        namespace=saga.namespace,
        version=comp_worker_version,
    )
    worker_snapshot = worker_snapshot_for_compensation(worker_row) if worker_row else None

    try:
        command = DoCompensationCommand(
            type=CommandType.EXECUTE_COMPENSATION,
            namespace=saga.namespace,
            saga_trace_id=saga.trace_id,
            step_span_id=new_span,
            worker_name=comp_worker,
            worker_version=comp_worker_version,
            idempotency_key=cmd_idempotency_key,
            forward_step_span_id=forward.span_id,
            original_input=resolved_comp_input,
            failure_reason=forward.error_details,
            tool_specs=slim_tool_specs(comp_tool_specs),
            resource_specs=cast("list[ResourceSpec]", comp_resource_specs),
            worker_snapshot=worker_snapshot,
        )
    except ValidationError as exc:
        logger.exception(
            "Failed to create CompensationCommand for saga %s step order %s: %s",
            saga.trace_id,
            forward.order_index,
            exc,
        )
        forward_from_status = status_value(forward.status)
        forward.status = StepStatus.FAILED
        forward.error_details = {"code": "COMPENSATION_BUILD_ERROR", "msg": str(exc)}
        await forward.save(using_db=db_conn)
        await get_registry().engine.on_step_transition(
            saga=saga,
            step=forward,
            from_status=forward_from_status,
            to_status=status_value(StepStatus.FAILED),
            conn=db_conn,
            trace_context=trace_context,
            event_type=AuditEngineEventType.STEP_FAILED,
            error_code="COMPENSATION_BUILD_ERROR",
            reason="compensation_build_error",
        )
        await _emit_saga_failed_from_compensation(
            saga,
            db_conn=db_conn,
            trace_context=trace_context,
            reason="compensation_failed",
            step_span_id=forward.span_id,
            error_details=forward.error_details,
        )
        return

    comp_step = await SagaStepInstance.create(
        span_id=new_span,
        compensates_span_id=forward.span_id,
        saga_trace_id=saga.trace_id,
        namespace=forward.namespace,
        saga=saga,
        step_id=forward.step_id,
        step_name=forward.step_name,
        order_index=forward.order_index,
        idempotency_key=cmd_idempotency_key,
        timeout_seconds=forward.timeout_seconds,
        max_turns=undo_max_turns,
        status=StepStatus.COMPENSATING,
        worker=comp_worker,
        worker_version=comp_worker_version,
        step_kind=forward.step_kind,
        tools_allow=comp_tool_specs,
        resources_allow=comp_resource_specs,
        parameters_spec={},
        resolved_arguments={},
        prompt_ref=None,
        output_payload=None,
        error_details=None,
        compensation_definition=forward.compensation_definition,
        output_schema=None,
        policy_name=None,
        pending_review_payload=None,
        using_db=db_conn,
    )
    await get_registry().engine.on_step_created(
        saga=saga,
        step=comp_step,
        conn=db_conn,
        compensates_span_id=forward.span_id,
    )
    await get_registry().engine.on_compensation_scheduled(
        saga=saga,
        step=comp_step,
        conn=db_conn,
        trace_context=trace_context,
        forward=forward,
        compensation_span_id=new_span,
    )

    logger.info("Queuing compensation command for step %s", forward.order_index)

    schedule_acc.stop("comp_schedule", bucket="schedule_ms")
    await emit_saga_event(
        topic=TOPIC_WORKER_COMMANDS,
        event_type=CommandType.EXECUTE_COMPENSATION.value,
        payload_schema=command,
        conn=db_conn,
    )
    await persist_pending_engine_timing(
        comp_step,
        engine_add=schedule_acc.to_dict(),
        dispatch_anchor=time.perf_counter(),
        conn=db_conn,
    )


async def _advance_lifo_compensation(
    saga: SagaInstance,
    from_order: int,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
) -> None:
    order = from_order
    while order >= 0:
        forward = await _load_forward_step_for_compensation(saga, order, db_conn)
        if forward is None:
            logger.error(
                "Saga %s missing forward step at order %s during compensation walk.",
                saga.trace_id,
                order,
            )
            await _emit_saga_failed_from_compensation(
                saga,
                db_conn=db_conn,
                trace_context=trace_context,
                reason="compensation_failed",
                error_details={"code": "MISSING_FORWARD_STEP", "order_index": order},
            )
            return

        if not forward_step_has_compensation(forward):
            logger.info(
                "Saga %s skipping compensation for order %s (no compensation declared).",
                saga.trace_id,
                order,
            )
            order -= 1
            continue

        if not forward_eligible_for_compensation(forward):
            logger.info(
                "Saga %s skipping compensation for order %s (status=%s not eligible).",
                saga.trace_id,
                order,
                forward.status,
            )
            order -= 1
            continue

        if await _undo_row_status_exists(saga, forward.span_id, StepStatus.COMPENSATED, db_conn):
            logger.info(
                "Saga %s forward span %s already compensated; advancing LIFO cursor.",
                saga.trace_id,
                forward.span_id,
            )
            order -= 1
            continue

        if await _undo_row_status_exists(saga, forward.span_id, StepStatus.COMPENSATING, db_conn):
            logger.warning(
                "Saga %s compensation already in flight for forward span %s.",
                saga.trace_id,
                forward.span_id,
            )
            return

        await _schedule_compensation_for_forward(
            saga,
            forward,
            db_conn,
            trace_context=trace_context,
        )
        return

    await _finalize_saga_compensated(saga, db_conn, trace_context=trace_context)


@trace_step()
async def trigger_compensation(
    saga: SagaInstance,
    step_order: int,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
):
    """Walk the LIFO compensation cursor from ``step_order`` and schedule or finalize.

    The forward row for each order is left unchanged; undo work is recorded on child rows
    with ``compensates_span_id`` referencing that forward row's ``span_id``.

    Args:
        saga: Saga instance (locked).
        step_order: Order index to start or continue the LIFO walk.
        db_conn: Transaction connection.
        trace_context: Optional trace context for hooks.

    Returns:
        None. Schedules one undo command, waits on in-flight undo, finalizes ``COMPENSATED``,
        or transitions saga to ``FAILED`` on unrecoverable schedule errors.
    """
    await _advance_lifo_compensation(
        saga,
        step_order,
        db_conn,
        trace_context=trace_context,
    )
