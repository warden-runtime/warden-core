"""Manifest-level loop helpers: freeze, materialize segments, until evaluation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from common.policy.cel_eval import (
    PolicyEvaluationError,
    compile_cel_program,
    evaluate_cel_bool,
)
from common.policy.loader import PolicyArtifact
from common.schemas.saga import (
    CommitSagaStep,
    ExecutableSagaStep,
    JoinSagasStep,
    LoopSagaStep,
    ReasonSagaStep,
    SpawnSagasStep,
    TopLevelSagaStep,
)
from common.utils import coerce_dict, status_value

if TYPE_CHECKING:
    from common.models import SagaInstance, SagaStepInstance

_TOP_LEVEL_ADAPTER = TypeAdapter(TopLevelSagaStep)
_EXECUTABLE_FORWARD_ADAPTER = TypeAdapter(ExecutableSagaStep)

_compiled_until_cache: dict[str, object] = {}

LOOP_STATUS_RUNNING = "running"
LOOP_STATUS_EXITED = "exited"
LOOP_STATUS_EXHAUSTED = "exhausted"


def validate_until_cel_compile(cel_source: str) -> None:
    """Compile-check loop ``until.cel``; raises PolicyEvaluationError on parse errors."""
    compile_cel_program(cel_source.strip())


def _compiled_until(cel_source: str) -> object:
    runner = _compiled_until_cache.get(cel_source)
    if runner is None:
        runner = compile_cel_program(cel_source)
        _compiled_until_cache[cel_source] = runner
    return runner


def parse_top_level_step(step_spec: dict[str, Any]) -> TopLevelSagaStep:
    """Validate one top-level blueprint step mapping."""
    return _TOP_LEVEL_ADAPTER.validate_python(step_spec)


def parse_executable_step(
    step_spec: dict[str, Any],
) -> ReasonSagaStep | CommitSagaStep | SpawnSagasStep | JoinSagasStep:
    """Validate one materializable forward step (reason/commit/spawn/join)."""
    return _EXECUTABLE_FORWARD_ADAPTER.validate_python(step_spec)


def initial_loops_context(frozen_steps: list[Any]) -> dict[str, Any]:
    """Build ``context.loops`` shells for every loop id in the frozen blueprint."""
    loops: dict[str, Any] = {}
    for raw in frozen_steps:
        if not isinstance(raw, dict) or raw.get("kind") != "loop":
            continue
        loop_id = str(raw.get("id") or "").strip()
        if not loop_id:
            continue
        max_iterations = int(raw.get("max_iterations") or 1)
        loops[loop_id] = {
            "iteration": 1,
            "max_iterations": max_iterations,
            "status": LOOP_STATUS_RUNNING,
        }
    return loops


def build_loop_definitions(frozen_steps: list[Any]) -> dict[str, Any]:
    """Freeze loop metadata keyed by loop id for running-saga immunity to redeploy."""
    defs: dict[str, Any] = {}
    for index, raw in enumerate(frozen_steps):
        if not isinstance(raw, dict) or raw.get("kind") != "loop":
            continue
        loop = parse_top_level_step(raw)
        if not isinstance(loop, LoopSagaStep):
            continue
        raw_until = raw.get("until")
        until: dict[str, Any] = raw_until if isinstance(raw_until, dict) else {}
        defs[loop.id] = {
            "id": loop.id,
            "name": loop.name or loop.id,
            "max_iterations": loop.max_iterations,
            "until_cel": str(until.get("cel") or loop.until.cel),
            "blueprint_index": index,
            "body": list(raw.get("steps") or []),
        }
    return defs


def take_initial_materialization_segment(
    frozen_steps: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return executable step specs for saga start and the initial loop_state cursor.

    Materializes prefix steps plus the first loop's iteration-1 body, then stops
    (delay-tail). When there is no loop, materializes the entire blueprint.
    """
    segment: list[dict[str, Any]] = []
    next_blueprint_index = 0
    active_loop_id: str | None = None

    for index, raw in enumerate(frozen_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"Saga step at index {index} must be a mapping")
        kind = raw.get("kind")
        if kind == "loop":
            body = raw.get("steps")
            if not isinstance(body, list) or not body:
                raise ValueError(f"Loop at index {index} has an empty body")
            loop_id = str(raw.get("id") or "").strip()
            for body_spec in body:
                if not isinstance(body_spec, dict):
                    raise ValueError(f"Loop {loop_id!r} body step must be a mapping")
                annotated = dict(body_spec)
                annotated["_loop_id"] = loop_id
                annotated["_loop_iteration"] = 1
                segment.append(annotated)
            active_loop_id = loop_id
            next_blueprint_index = index + 1
            break
        segment.append(dict(raw))
        next_blueprint_index = index + 1

    loop_state = {
        "next_blueprint_index": next_blueprint_index,
        "active_loop_id": active_loop_id,
    }
    return segment, loop_state


def take_next_materialization_segment(
    frozen_steps: list[Any],
    *,
    from_blueprint_index: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize the next blueprint segment after a loop exits (or continue linear)."""
    segment: list[dict[str, Any]] = []
    next_blueprint_index = from_blueprint_index
    active_loop_id: str | None = None

    for index in range(from_blueprint_index, len(frozen_steps)):
        raw = frozen_steps[index]
        if not isinstance(raw, dict):
            raise ValueError(f"Saga step at index {index} must be a mapping")
        kind = raw.get("kind")
        if kind == "loop":
            body = raw.get("steps")
            if not isinstance(body, list) or not body:
                raise ValueError(f"Loop at index {index} has an empty body")
            loop_id = str(raw.get("id") or "").strip()
            for body_spec in body:
                if not isinstance(body_spec, dict):
                    raise ValueError(f"Loop {loop_id!r} body step must be a mapping")
                annotated = dict(body_spec)
                annotated["_loop_id"] = loop_id
                annotated["_loop_iteration"] = 1
                segment.append(annotated)
            active_loop_id = loop_id
            next_blueprint_index = index + 1
            break
        segment.append(dict(raw))
        next_blueprint_index = index + 1

    loop_state = {
        "next_blueprint_index": next_blueprint_index,
        "active_loop_id": active_loop_id,
    }
    return segment, loop_state


def mint_loop_iteration_body(
    loop_def: dict[str, Any],
    *,
    iteration: int,
) -> list[dict[str, Any]]:
    """Build annotated body step specs for loop iteration N."""
    loop_id = str(loop_def.get("id") or "")
    body = loop_def.get("body") or []
    if not isinstance(body, list):
        raise ValueError(f"Loop {loop_id!r} body is invalid")
    segment: list[dict[str, Any]] = []
    for body_spec in body:
        if not isinstance(body_spec, dict):
            raise ValueError(f"Loop {loop_id!r} body step must be a mapping")
        annotated = dict(body_spec)
        annotated["_loop_id"] = loop_id
        annotated["_loop_iteration"] = iteration
        segment.append(annotated)
    return segment


def loop_body_step_ids(loop_def: dict[str, Any]) -> list[str]:
    """Ordered body step ids for a frozen loop definition."""
    body = loop_def.get("body") or []
    ids: list[str] = []
    for raw in body:
        if isinstance(raw, dict) and raw.get("id"):
            ids.append(str(raw["id"]))
    return ids


def is_last_body_step(step: SagaStepInstance, loop_def: dict[str, Any]) -> bool:
    """True when ``step`` is the last body step of its loop definition."""
    ids = loop_body_step_ids(loop_def)
    if not ids:
        return False
    return step.step_id == ids[-1]


def until_binding(
    *,
    saga: SagaInstance,
    loop_id: str,
    iteration: int,
    max_iterations: int,
) -> dict[str, Any]:
    """CEL root binding for ``until.cel`` evaluation."""
    ctx = coerce_dict(saga.context)
    steps_ctx = ctx.get("steps")
    if not isinstance(steps_ctx, dict):
        steps_ctx = {}
    loops_ctx = ctx.get("loops")
    if not isinstance(loops_ctx, dict):
        loops_ctx = {}
    return {
        "input": coerce_dict(ctx.get("input")),
        "steps": steps_ctx,
        "loops": loops_ctx,
        "saga": {
            "trace_id": saga.trace_id,
            "namespace": saga.namespace,
            "status": status_value(saga.status),
        },
        "loop": {
            "id": loop_id,
            "iteration": iteration,
            "max_iterations": max_iterations,
        },
    }


def evaluate_until(*, cel_source: str, binding: dict[str, Any]) -> bool:
    """Evaluate ``until.cel`` to a bool; raises PolicyEvaluationError on eval errors."""
    source = cel_source.strip()
    artifact = PolicyArtifact(name="loop-until", version="1", cel_source=source)
    return evaluate_cel_bool(
        artifact=artifact,
        cel_program=_compiled_until(source),
        binding=binding,
    )


def set_loop_context_status(
    context: dict[str, Any],
    loop_id: str,
    *,
    iteration: int | None = None,
    status: str | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Update ``context.loops.<loop_id>`` and return the mutated context dict."""
    loops = dict(context.get("loops") or {})
    entry = dict(loops.get(loop_id) or {})
    if iteration is not None:
        entry["iteration"] = iteration
    if status is not None:
        entry["status"] = status
    if max_iterations is not None:
        entry["max_iterations"] = max_iterations
    loops[loop_id] = entry
    context["loops"] = loops
    return context


def forward_idempotency_key(
    *,
    trace_id: str,
    step_id: str,
    loop_id: str | None,
    iteration: int | None,
) -> str:
    """Idempotency key for a forward step row (iteration-aware inside loops)."""
    if loop_id and iteration is not None:
        return f"{trace_id}-{step_id}-{loop_id}-{iteration}"
    return f"{trace_id}-{step_id}"


__all__ = [
    "LOOP_STATUS_EXHAUSTED",
    "LOOP_STATUS_EXITED",
    "LOOP_STATUS_RUNNING",
    "PolicyEvaluationError",
    "build_loop_definitions",
    "evaluate_until",
    "forward_idempotency_key",
    "initial_loops_context",
    "is_last_body_step",
    "loop_body_step_ids",
    "mint_loop_iteration_body",
    "parse_executable_step",
    "parse_top_level_step",
    "set_loop_context_status",
    "take_initial_materialization_segment",
    "take_next_materialization_segment",
    "until_binding",
    "validate_until_cel_compile",
]
