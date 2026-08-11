"""Engine-native spawn_sagas / join_sagas orchestration."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from common.child_sagas import (
    CHILD_TERMINAL,
    build_child_resolve_context,
    child_start_idempotency_key,
    jsonpath_first,
    resolve_items_from_context,
    summarize_join_children,
    validate_spawn_items,
)
from common.config import get_settings
from common.contracts import StepFailedEvent
from common.loops import parse_executable_step
from common.models import (
    EventType,
    SagaChild,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
)
from common.schemas.saga import JoinSagasStep, SpawnSagasStep
from common.step_output import wrap_step_output_data
from common.utils import status_value

from engine.api.saga_start import (
    _create_saga_and_steps,
    _existing_start_trace_id,
    _require_saga_definition,
)
from engine.utils import resolve_parameters_spec

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient

logger = logging.getLogger(__name__)


def frozen_step_spec(saga: SagaInstance, step_id: str) -> dict[str, Any] | None:
    """Return the frozen blueprint mapping for ``step_id`` (top-level only)."""
    for raw in saga.frozen_steps or []:
        if isinstance(raw, dict) and raw.get("id") == step_id:
            return dict(raw)
    return None


def parse_spawn_step(saga: SagaInstance, step_id: str) -> SpawnSagasStep:
    raw = frozen_step_spec(saga, step_id)
    if raw is None:
        raise ValueError(f"spawn step {step_id!r} not found in frozen_steps")
    parsed = parse_executable_step(raw)
    if not isinstance(parsed, SpawnSagasStep):
        raise ValueError(f"step {step_id!r} is not kind spawn_sagas")
    return parsed


def parse_join_step(saga: SagaInstance, step_id: str) -> JoinSagasStep:
    raw = frozen_step_spec(saga, step_id)
    if raw is None:
        raise ValueError(f"join step {step_id!r} not found in frozen_steps")
    parsed = parse_executable_step(raw)
    if not isinstance(parsed, JoinSagasStep):
        raise ValueError(f"step {step_id!r} is not kind join_sagas")
    return parsed


async def execute_spawn_sagas(
    *,
    saga: SagaInstance,
    step: SagaStepInstance,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Create child sagas for each item and complete the spawn step."""
    from engine.logic import _apply_step_failure_lifecycle, _finalize_step_output_and_advance

    try:
        spawn_model = parse_spawn_step(saga, step.step_id)
        spec = spawn_model.spawn
        items = resolve_items_from_context(spec.items_from, saga.context or {})
        validated = validate_spawn_items(items, max_children=spec.max_children)
    except ValueError as exc:
        msg = str(exc)
        code = "SPAWN_VALIDATION_ERROR"
        for known in (
            "SPAWN_EMPTY_ITEMS",
            "TOO_MANY_CHILDREN",
            "SPAWN_ITEM_MISSING_ID",
            "SPAWN_RESULT_FROM_REQUIRED",
        ):
            if known in msg:
                code = known
                break
        synthetic = StepFailedEvent(
            saga_trace_id=saga.trace_id,
            namespace=saga.namespace,
            event_type=EventType.STEP_FAILED.value,
            step_span_id=step.span_id,
            error_details={"code": code, "message": msg},
        )
        await _apply_step_failure_lifecycle(saga, step, synthetic, db_conn)
        return

    settings = get_settings()
    children_out: list[dict[str, str]] = []
    for item_id, item in validated:
        idem_key = child_start_idempotency_key(saga.trace_id, step.step_id, item_id)
        existing = await _existing_start_trace_id(
            namespace=saga.namespace,
            idempotency_key=idem_key,
            conn=db_conn,
        )
        if existing is not None:
            child_trace_id = existing
        else:
            resolve_ctx = build_child_resolve_context(
                saga.context or {},
                item,
                item_var=spec.item_var,
            )
            input_map = {
                key: entry.model_dump(by_alias=True, exclude_none=True)
                for key, entry in spec.input.items()
            }
            child_input = resolve_parameters_spec(input_map, resolve_ctx)
            definition = await _require_saga_definition(
                namespace=saga.namespace,
                name=spec.saga_name,
                version=spec.saga_version,
                conn=db_conn,
            )
            child_trace_id = await _create_saga_and_steps(
                conn=db_conn,
                definition=definition,
                namespace=saga.namespace,
                name=spec.saga_name,
                version=spec.saga_version,
                input=child_input,
                idempotency_key=idem_key,
                schemas_root=settings.schemas_root,
                compensations_root=settings.compensations_root,
                parent_trace_id=saga.trace_id,
            )

        link = await SagaChild.filter(idempotency_key=idem_key).using_db(db_conn).first()
        if link is None:
            await SagaChild.create(
                id=uuid.uuid4(),
                namespace=saga.namespace,
                parent_trace_id=saga.trace_id,
                spawn_step_id=step.step_id,
                spawn_span_id=step.span_id,
                item_id=item_id,
                child_trace_id=child_trace_id,
                idempotency_key=idem_key,
                using_db=db_conn,
            )
        children_out.append({"item_id": item_id, "child_trace_id": child_trace_id})

    await _finalize_step_output_and_advance(
        saga,
        step,
        wrap_step_output_data({"spawned": len(children_out), "children": children_out}),
        db_conn,
        trace_context=trace_context,
    )


async def _child_error_details(child: SagaInstance, db_conn: BaseDBAsyncClient) -> dict[str, Any]:
    failed_step = (
        await SagaStepInstance.filter(
            saga_trace_id=child.trace_id,
            compensates_span_id__isnull=True,
            status__in=[StepStatus.FAILED, StepStatus.TIMED_OUT],
        )
        .using_db(db_conn)
        .order_by("-forward_seq")
        .first()
    )
    if failed_step is not None and isinstance(failed_step.error_details, dict):
        return dict(failed_step.error_details)
    return {
        "code": status_value(child.status),
        "message": f"Child saga ended with status {status_value(child.status)}",
    }


async def build_join_payload(
    *,
    parent: SagaInstance,
    join_step: SagaStepInstance,
    db_conn: BaseDBAsyncClient,
) -> dict[str, Any]:
    """Assemble join output data from linked children (wait_all assumption)."""
    join_model = parse_join_step(parent, join_step.step_id)
    spawn_step_id = join_model.join.spawn_step_id
    spawn_model = parse_spawn_step(parent, spawn_step_id)
    result_from = spawn_model.spawn.result_from

    links = (
        await SagaChild.filter(
            namespace=parent.namespace,
            parent_trace_id=parent.trace_id,
            spawn_step_id=spawn_step_id,
        )
        .using_db(db_conn)
        .order_by("created_at")
        .all()
    )
    children_payload: list[dict[str, Any]] = []
    status_rows: list[dict[str, str]] = []
    for link in links:
        child = await SagaInstance.filter(trace_id=link.child_trace_id).using_db(db_conn).first()
        if child is None:
            status = SagaStatus.FAILED.value
            entry: dict[str, Any] = {
                "item_id": link.item_id,
                "child_trace_id": link.child_trace_id,
                "status": status,
                "output": None,
                "error": {
                    "code": "CHILD_MISSING",
                    "message": f"Child saga {link.child_trace_id} not found",
                },
            }
        else:
            status = status_value(child.status)
            if status == SagaStatus.COMPLETED.value:
                output = jsonpath_first(child.context or {}, result_from)
                entry = {
                    "item_id": link.item_id,
                    "child_trace_id": child.trace_id,
                    "status": status,
                    "output": output,
                    "error": None,
                }
            else:
                entry = {
                    "item_id": link.item_id,
                    "child_trace_id": child.trace_id,
                    "status": status,
                    "output": None,
                    "error": await _child_error_details(child, db_conn),
                }
        children_payload.append(entry)
        status_rows.append({"status": status})

    summary = summarize_join_children(status_rows)
    return {"summary": summary, "children": children_payload}


def _all_children_terminal(children: list[SagaInstance]) -> bool:
    if not children:
        return False
    for child in children:
        if status_value(child.status) not in CHILD_TERMINAL:
            return False
    return True


async def load_spawn_children(
    *,
    parent_trace_id: str,
    spawn_step_id: str,
    namespace: str,
    db_conn: BaseDBAsyncClient,
) -> list[SagaInstance]:
    links = (
        await SagaChild.filter(
            namespace=namespace,
            parent_trace_id=parent_trace_id,
            spawn_step_id=spawn_step_id,
        )
        .using_db(db_conn)
        .all()
    )
    if not links:
        return []
    trace_ids = [link.child_trace_id for link in links]
    return await SagaInstance.filter(trace_id__in=trace_ids).using_db(db_conn).all()


async def try_complete_join(
    *,
    parent: SagaInstance,
    join_step: SagaStepInstance,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None = None,
) -> bool:
    """Complete join when all children are terminal. Returns True if completed/failed."""
    from engine.logic import _apply_step_failure_lifecycle, _finalize_step_output_and_advance

    if join_step.status != StepStatus.IN_PROGRESS:
        return False

    join_model = parse_join_step(parent, join_step.step_id)
    children = await load_spawn_children(
        parent_trace_id=parent.trace_id,
        spawn_step_id=join_model.join.spawn_step_id,
        namespace=parent.namespace,
        db_conn=db_conn,
    )
    if not _all_children_terminal(children):
        return False

    payload = await build_join_payload(parent=parent, join_step=join_step, db_conn=db_conn)
    summary = payload.get("summary") or {}
    if not join_model.join.allow_zero_success and int(summary.get("succeeded") or 0) == 0:
        synthetic = StepFailedEvent(
            saga_trace_id=parent.trace_id,
            namespace=parent.namespace,
            event_type=EventType.STEP_FAILED.value,
            step_span_id=join_step.span_id,
            error_details={
                "code": "ALL_CHILDREN_FAILED",
                "message": "join_sagas: no children completed successfully",
                "summary": summary,
            },
        )
        await _apply_step_failure_lifecycle(parent, join_step, synthetic, db_conn)
        return True

    await _finalize_step_output_and_advance(
        parent,
        join_step,
        wrap_step_output_data(payload),
        db_conn,
        trace_context=trace_context,
    )
    return True


async def park_or_complete_join(
    *,
    saga: SagaInstance,
    step: SagaStepInstance,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Join entrypoint from trigger_step: park IN_PROGRESS or complete immediately."""
    completed = await try_complete_join(
        parent=saga,
        join_step=step,
        db_conn=db_conn,
        trace_context=trace_context,
    )
    if completed:
        logger.info(
            "join_sagas %s completed immediately for saga %s",
            step.step_id,
            saga.trace_id,
        )
    else:
        logger.info(
            "join_sagas %s parked (waiting on children) for saga %s",
            step.step_id,
            saga.trace_id,
        )


async def on_child_saga_terminal(
    child: SagaInstance,
    db_conn: BaseDBAsyncClient,
    *,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Wake parent join and/or resume spawn compensation after a child terminals."""
    parent_trace_id = child.parent_trace_id
    if not parent_trace_id:
        return

    parent = (
        await SagaInstance.filter(trace_id=parent_trace_id)
        .using_db(db_conn)
        .select_for_update()
        .first()
    )
    if parent is None:
        return

    join_step = (
        await SagaStepInstance.filter(
            saga_trace_id=parent.trace_id,
            step_kind="join_sagas",
            status=StepStatus.IN_PROGRESS,
            compensates_span_id__isnull=True,
        )
        .using_db(db_conn)
        .select_for_update()
        .first()
    )
    if join_step is not None:
        await try_complete_join(
            parent=parent,
            join_step=join_step,
            db_conn=db_conn,
            trace_context=trace_context,
        )

    if parent.status == SagaStatus.COMPENSATING:
        await maybe_resume_spawn_compensation(
            parent=parent,
            db_conn=db_conn,
            trace_context=trace_context,
        )


async def children_all_terminal_for_spawn(
    *,
    parent: SagaInstance,
    spawn_step_id: str,
    db_conn: BaseDBAsyncClient,
) -> bool:
    children = await load_spawn_children(
        parent_trace_id=parent.trace_id,
        spawn_step_id=spawn_step_id,
        namespace=parent.namespace,
        db_conn=db_conn,
    )
    return _all_children_terminal(children)


async def compensate_spawn_sagas_forward(
    *,
    saga: SagaInstance,
    forward: SagaStepInstance,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None = None,
) -> bool:
    """Engine-native spawn compensation: wait until children terminal, then mark done.

    Returns True when spawn is fully compensated and LIFO may advance; False when parked.
    """
    from engine.logic import _advance_lifo_compensation

    if not await children_all_terminal_for_spawn(
        parent=saga,
        spawn_step_id=forward.step_id,
        db_conn=db_conn,
    ):
        logger.info(
            "Saga %s spawn compensation parked waiting for children of %s",
            saga.trace_id,
            forward.step_id,
        )
        return False

    # Mark a synthetic compensated undo without worker: create COMPENSATED undo row
    # if missing, then advance LIFO past this forward_seq.
    existing = (
        await SagaStepInstance.filter(
            saga_trace_id=saga.trace_id,
            compensates_span_id=forward.span_id,
            status=StepStatus.COMPENSATED,
        )
        .using_db(db_conn)
        .first()
    )
    if existing is None:
        await SagaStepInstance.create(
            span_id=uuid.uuid4().hex[:16],
            compensates_span_id=forward.span_id,
            saga_trace_id=saga.trace_id,
            namespace=forward.namespace,
            saga=saga,
            step_id=forward.step_id,
            step_name=forward.step_name,
            order_index=forward.order_index,
            forward_seq=forward.forward_seq,
            loop_id=forward.loop_id,
            iteration=forward.iteration,
            idempotency_key=f"{forward.idempotency_key}:spawn-compensate",
            timeout_seconds=forward.timeout_seconds,
            max_turns=forward.max_turns,
            status=StepStatus.COMPENSATED,
            worker=forward.worker,
            worker_version=forward.worker_version,
            step_kind=forward.step_kind,
            tools_allow=[],
            resources_allow=[],
            parameters_spec={},
            resolved_arguments={},
            prompt_ref=None,
            output_payload={"data": {"awaited_children": True}},
            error_details=None,
            compensation_definition=None,
            output_schema=None,
            policy_name=None,
            pending_review_payload=None,
            using_db=db_conn,
        )

    await _advance_lifo_compensation(
        saga,
        forward.forward_seq - 1,
        db_conn,
        trace_context=trace_context,
    )
    return True


async def maybe_resume_spawn_compensation(
    *,
    parent: SagaInstance,
    db_conn: BaseDBAsyncClient,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """If parent is COMPENSATING on a spawn wait, try to resume LIFO."""
    if parent.status != SagaStatus.COMPENSATING:
        return
    spawn_forward = (
        await SagaStepInstance.filter(
            saga_trace_id=parent.trace_id,
            step_kind="spawn_sagas",
            compensates_span_id__isnull=True,
            status__in=[
                StepStatus.COMPLETED,
                StepStatus.FAILED,
                StepStatus.TIMED_OUT,
                StepStatus.IN_PROGRESS,
            ],
        )
        .using_db(db_conn)
        .order_by("-forward_seq")
        .first()
    )
    if spawn_forward is None:
        return
    already = (
        await SagaStepInstance.filter(
            saga_trace_id=parent.trace_id,
            compensates_span_id=spawn_forward.span_id,
            status=StepStatus.COMPENSATED,
        )
        .using_db(db_conn)
        .exists()
    )
    if already:
        return
    await compensate_spawn_sagas_forward(
        saga=parent,
        forward=spawn_forward,
        db_conn=db_conn,
        trace_context=trace_context,
    )
