"""Loop exit / continue / exhaust control after a body pass completes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from common.error_details import build_step_error_details
from common.loops import (
    LOOP_STATUS_EXHAUSTED,
    LOOP_STATUS_EXITED,
    LOOP_STATUS_RUNNING,
    PolicyEvaluationError,
    evaluate_until,
    is_last_body_step,
    mint_loop_iteration_body,
    set_loop_context_status,
    take_next_materialization_segment,
    until_binding,
)
from common.models import SagaInstance, SagaStepInstance
from common.utils import coerce_dict

from engine.step_materialize import materialize_executable_steps

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient

logger = logging.getLogger(__name__)

ScheduleNext = Callable[..., Awaitable[None]]


async def _max_forward_seq(saga: SagaInstance, *, db_conn: BaseDBAsyncClient) -> int:
    row = (
        await SagaStepInstance.filter(
            saga_trace_id=saga.trace_id,
            compensates_span_id__isnull=True,
        )
        .using_db(db_conn)
        .order_by("-forward_seq")
        .first()
    )
    return int(row.forward_seq) if row is not None else -1


async def _mint_and_schedule(
    saga: SagaInstance,
    step_specs: list[dict[str, Any]],
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    schedule_next: ScheduleNext,
) -> None:
    max_seq = await _max_forward_seq(saga, db_conn=db_conn)
    created = await materialize_executable_steps(
        saga=saga,
        step_specs=step_specs,
        start_forward_seq=max_seq + 1,
        start_order_index=max_seq + 1,
        conn=db_conn,
    )
    await schedule_next(
        saga,
        after_seq=created[0].forward_seq - 1,
        db_conn=db_conn,
        trace_context=trace_context,
    )


async def _fail_saga_after_successful_body(
    saga: SagaInstance,
    step: SagaStepInstance,
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    error_details: dict[str, Any],
) -> None:
    """Terminal loop failure after a clean body pass: compensate including ``step``."""
    from common.models import SagaStatus
    from common.plugins.registry import get_registry
    from common.schemas.engine_events import AuditEngineEventType
    from common.utils import status_value

    from engine.logic import trigger_compensation

    prior = saga.status
    saga.status = SagaStatus.COMPENSATING
    await saga.save(using_db=db_conn)
    await get_registry().engine.on_saga_transition(
        saga=saga,
        from_status=status_value(prior),
        to_status=status_value(SagaStatus.COMPENSATING),
        conn=db_conn,
        trace_context=trace_context,
        event_type=AuditEngineEventType.SAGA_COMPENSATING,
        reason=str(error_details.get("code") or "loop_terminal"),
    )
    if not step.error_details:
        step.error_details = error_details
        await step.save(using_db=db_conn, update_fields=["error_details"])

    await trigger_compensation(
        saga,
        forward_seq=step.forward_seq,
        db_conn=db_conn,
        trace_context=trace_context,
    )


async def _exit_loop(
    saga: SagaInstance,
    step: SagaStepInstance,
    *,
    loop_id: str,
    ctx: dict[str, Any],
    iteration: int,
    max_iterations: int,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    schedule_next: ScheduleNext,
) -> None:
    set_loop_context_status(
        ctx,
        loop_id,
        iteration=iteration,
        status=LOOP_STATUS_EXITED,
        max_iterations=max_iterations,
    )
    saga.context = ctx
    frozen_steps = list(saga.frozen_steps or [])
    loop_state = coerce_dict(saga.loop_state)
    from_index = int(loop_state.get("next_blueprint_index") or 0)
    segment, new_loop_state = take_next_materialization_segment(
        frozen_steps,
        from_blueprint_index=from_index,
    )
    saga.loop_state = new_loop_state
    await saga.save(using_db=db_conn)

    if not segment:
        await schedule_next(
            saga,
            after_seq=step.forward_seq,
            db_conn=db_conn,
            trace_context=trace_context,
        )
        return

    active = (new_loop_state or {}).get("active_loop_id")
    if active:
        set_loop_context_status(ctx, str(active), iteration=1, status=LOOP_STATUS_RUNNING)
        saga.context = ctx
        await saga.save(using_db=db_conn)

    await _mint_and_schedule(
        saga,
        segment,
        db_conn=db_conn,
        trace_context=trace_context,
        schedule_next=schedule_next,
    )


async def _continue_loop(
    saga: SagaInstance,
    step: SagaStepInstance,
    loop_def: dict[str, Any],
    *,
    loop_id: str,
    ctx: dict[str, Any],
    next_iteration: int,
    max_iterations: int,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    schedule_next: ScheduleNext,
) -> None:
    set_loop_context_status(
        ctx,
        loop_id,
        iteration=next_iteration,
        status=LOOP_STATUS_RUNNING,
        max_iterations=max_iterations,
    )
    saga.context = ctx
    await saga.save(using_db=db_conn)
    body_specs = mint_loop_iteration_body(loop_def, iteration=next_iteration)
    await _mint_and_schedule(
        saga,
        body_specs,
        db_conn=db_conn,
        trace_context=trace_context,
        schedule_next=schedule_next,
    )


async def _evaluate_until_or_fail(
    saga: SagaInstance,
    step: SagaStepInstance,
    *,
    loop_id: str,
    iteration: int,
    max_iterations: int,
    until_cel: str,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
) -> bool | None:
    """Return until result, or ``None`` after failing the saga on eval error."""
    try:
        return evaluate_until(
            cel_source=until_cel,
            binding=until_binding(
                saga=saga,
                loop_id=loop_id,
                iteration=iteration,
                max_iterations=max_iterations,
            ),
        )
    except PolicyEvaluationError as e:
        ctx = dict(saga.context) if saga.context else {"input": {}, "steps": {}, "loops": {}}
        set_loop_context_status(
            ctx,
            loop_id,
            iteration=iteration,
            status=LOOP_STATUS_EXHAUSTED,
            max_iterations=max_iterations,
        )
        saga.context = ctx
        await saga.save(using_db=db_conn)
        await _fail_saga_after_successful_body(
            saga,
            step,
            db_conn=db_conn,
            trace_context=trace_context,
            error_details=build_step_error_details(
                code="UNTIL_EVALUATION_FAILED",
                message=str(e),
                loop_id=loop_id,
                iteration=iteration,
            ),
        )
        return None


async def handle_possible_loop_boundary(
    saga: SagaInstance,
    step: SagaStepInstance,
    *,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None,
    schedule_next: ScheduleNext,
) -> bool:
    """If ``step`` ends a loop body, evaluate until / mint / exit.

    Returns True when this function took ownership of scheduling (caller must not
    call the default forward_seq advance). Returns False to use normal advance.
    """
    loop_id = step.loop_id
    if not loop_id:
        return False

    loop_def = coerce_dict(saga.loop_definitions).get(loop_id)
    if not isinstance(loop_def, dict):
        logger.error(
            "Saga %s missing loop_definitions for loop_id=%s",
            saga.trace_id,
            loop_id,
        )
        return False
    if not is_last_body_step(step, loop_def):
        return False

    iteration = int(step.iteration or 1)
    max_iterations = int(loop_def.get("max_iterations") or 1)
    until_cel = str(loop_def.get("until_cel") or "").strip()
    should_exit = await _evaluate_until_or_fail(
        saga,
        step,
        loop_id=loop_id,
        iteration=iteration,
        max_iterations=max_iterations,
        until_cel=until_cel,
        db_conn=db_conn,
        trace_context=trace_context,
    )
    if should_exit is None:
        return True

    ctx = dict(saga.context) if saga.context else {"input": {}, "steps": {}, "loops": {}}
    if should_exit:
        await _exit_loop(
            saga,
            step,
            loop_id=loop_id,
            ctx=ctx,
            iteration=iteration,
            max_iterations=max_iterations,
            db_conn=db_conn,
            trace_context=trace_context,
            schedule_next=schedule_next,
        )
        return True

    if iteration >= max_iterations:
        set_loop_context_status(
            ctx,
            loop_id,
            iteration=iteration,
            status=LOOP_STATUS_EXHAUSTED,
            max_iterations=max_iterations,
        )
        saga.context = ctx
        await saga.save(using_db=db_conn)
        await _fail_saga_after_successful_body(
            saga,
            step,
            db_conn=db_conn,
            trace_context=trace_context,
            error_details=build_step_error_details(
                code="LOOP_EXHAUSTED",
                message=(
                    f"Loop {loop_id!r} reached max_iterations={max_iterations} "
                    "without until.cel becoming true"
                ),
                loop_id=loop_id,
                iteration=iteration,
                max_iterations=max_iterations,
            ),
        )
        return True

    await _continue_loop(
        saga,
        step,
        loop_def,
        loop_id=loop_id,
        ctx=ctx,
        next_iteration=iteration + 1,
        max_iterations=max_iterations,
        db_conn=db_conn,
        trace_context=trace_context,
        schedule_next=schedule_next,
    )
    return True
