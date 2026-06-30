"""Evaluate per-step ``when.cel`` schedule gates against saga context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.policy.cel_eval import (
    PolicyEvaluationError,
    compile_cel_program,
    evaluate_cel_bool,
)
from common.policy.loader import PolicyArtifact
from common.utils import coerce_dict, status_value

if TYPE_CHECKING:
    from common.models import SagaInstance, SagaStepInstance

_compiled_when_cache: dict[str, object] = {}


def validate_when_cel_compile(cel_source: str) -> None:
    """Compile-check step ``when.cel``; raises PolicyEvaluationError on parse errors."""
    compile_cel_program(cel_source.strip())


def _compiled_when(cel_source: str) -> object:
    runner = _compiled_when_cache.get(cel_source)
    if runner is None:
        runner = compile_cel_program(cel_source)
        _compiled_when_cache[cel_source] = runner
    return runner


def step_when_binding(*, saga: SagaInstance, step: SagaStepInstance) -> dict[str, Any]:
    """Build the CEL root binding evaluated before a forward step is scheduled."""
    ctx = coerce_dict(saga.context)
    steps_ctx = ctx.get("steps")
    if not isinstance(steps_ctx, dict):
        steps_ctx = {}
    return {
        "input": coerce_dict(ctx.get("input")),
        "steps": steps_ctx,
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
    }


def evaluate_step_when(*, cel_source: str, binding: dict[str, Any]) -> bool:
    """Evaluate ``when.cel`` to a bool; raises PolicyEvaluationError on eval errors."""
    source = cel_source.strip()
    artifact = PolicyArtifact(name="step-when", version="1", cel_source=source)
    return evaluate_cel_bool(
        artifact=artifact,
        cel_program=_compiled_when(source),
        binding=binding,
    )


__all__ = [
    "PolicyEvaluationError",
    "evaluate_step_when",
    "step_when_binding",
    "validate_when_cel_compile",
]
