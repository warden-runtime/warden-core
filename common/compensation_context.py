"""Shared compensation context hydration (engine scheduling and worker execution)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.models import SagaInstance, SagaStepInstance

from common.models import StepStatus
from common.step_output import business_data_from_step_output, wrap_step_output_data

# JSONPath / saga context key for rollback metadata (safe defaults on dirty failures).
COMPENSATION_METADATA_KEY = "_compensation"

# Injected into MCP tool arguments so external undo APIs can dedupe redelivered commands.
WARDEN_TOOL_IDEMPOTENCY_KEY = "warden_idempotency_key"

DEFAULT_COMPENSATION_PROMPT = (
    "[CRITICAL POLICY]\n"
    "You are running an automated system rollback. Do not diagnose, fix, or retry the "
    "forward operation. Your sole objective is to execute the provided cleanup tools using "
    "the parameters in original_input. Use warden_idempotency_key when the undo API supports "
    "idempotency.\n"
    "If forward step output is missing (blind_cleanup), use only original_input and saga input."
)


def step_output_for_saga_context(output: dict[str, Any] | None) -> dict[str, Any]:
    """Canonical ``{ \"data\": <dict> }`` for ``context.steps`` (drops non-data envelope keys)."""
    raw = output if isinstance(output, dict) else {}
    inner = business_data_from_step_output(raw)
    return wrap_step_output_data(dict(inner) if inner is not None else {})


def effective_forward_step_output(step: SagaStepInstance) -> dict[str, Any] | None:
    """Output payload used when compensating a forward step."""
    out = step.output_payload or step.pending_review_payload
    return out if isinstance(out, dict) else None


def forward_step_has_rollback_output(step: SagaStepInstance) -> bool:
    """True when the forward row has structured output usable for JSONPath rollback."""
    merged = effective_forward_step_output(step)
    if not merged:
        return False
    inner = business_data_from_step_output(merged)
    return bool(inner) if inner is not None else bool(merged)


def is_dirty_forward_step(step: SagaStepInstance) -> bool:
    """Forward step failed without a reliable completed output (timeout / crash)."""
    if step.status == StepStatus.TIMED_OUT:
        return True
    details = step.error_details if isinstance(step.error_details, dict) else {}
    return details.get("code") == "SYSTEM_CRASH"


def build_compensation_metadata(
    forward: SagaStepInstance,
    *,
    undo_span_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Structured rollback metadata for context, prompts, and tool fencing."""
    has_output = forward_step_has_rollback_output(forward)
    dirty = is_dirty_forward_step(forward)
    return {
        "undo_span_id": undo_span_id,
        "forward_span_id": forward.span_id,
        "forward_step_id": forward.step_id,
        "forward_order_index": forward.order_index,
        "forward_status": str(forward.status),
        "idempotency_key": idempotency_key,
        "has_forward_output": has_output,
        "blind_cleanup": not has_output,
        "dirty_failure": dirty,
    }


def _attach_forward_step_output_layer(
    ctx: dict[str, Any],
    step: SagaStepInstance,
) -> None:
    """Ensure ``context.steps[step_id].output`` exists for JSONPath resolution."""
    if not step.step_id:
        return
    merged = step.output_payload or step.pending_review_payload
    steps_layer: dict[str, Any] = dict(ctx.get("steps") or {})
    entry: dict[str, Any] = dict(steps_layer.get(step.step_id) or {})
    entry["output"] = step_output_for_saga_context(merged if isinstance(merged, dict) else None)
    steps_layer[step.step_id] = entry
    ctx["steps"] = steps_layer


def compensation_parameter_context(
    saga: SagaInstance,
    step: SagaStepInstance,
    *,
    undo_span_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build saga-like context for resolving compensation ``with`` JSONPaths."""
    ctx: dict[str, Any] = dict(saga.context or {})
    _attach_forward_step_output_layer(ctx, step)
    if undo_span_id and idempotency_key:
        ctx[COMPENSATION_METADATA_KEY] = build_compensation_metadata(
            step,
            undo_span_id=undo_span_id,
            idempotency_key=idempotency_key,
        )
    return ctx


def merge_compensation_tool_arguments(
    llm_args: dict[str, Any] | None,
    original_input: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Merge LLM tool args with engine-resolved input and inject rollback idempotency key."""
    merged = dict(original_input or {})
    for key, value in (llm_args or {}).items():
        if value is not None and value != "":
            merged[key] = value
    if idempotency_key and WARDEN_TOOL_IDEMPOTENCY_KEY not in merged:
        merged[WARDEN_TOOL_IDEMPOTENCY_KEY] = idempotency_key
    return merged


def worker_snapshot_for_compensation(worker: Any) -> dict[str, Any]:
    """Freeze worker fields needed for undo at compensation schedule time."""
    return {
        "version": worker.version,
        "system_prompt": worker.system_prompt,
        "compensation_prompt": worker.compensation_prompt,
        "model_provider": worker.model_provider,
        "model_name": worker.model_name,
        "adapter": worker.adapter,
    }


def compensation_prompt_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    fallback: str | None,
    default: str = DEFAULT_COMPENSATION_PROMPT,
) -> str:
    """Resolve compensation prompt from command snapshot, worker row, or default."""
    if snapshot:
        snap_prompt = snapshot.get("compensation_prompt")
        if isinstance(snap_prompt, str) and snap_prompt.strip():
            return snap_prompt
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    return default


def system_prompt_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    fallback: str,
) -> str:
    """Resolve system prompt from command snapshot or live worker row."""
    if snapshot:
        snap_prompt = snapshot.get("system_prompt")
        if isinstance(snap_prompt, str) and snap_prompt.strip():
            return snap_prompt
    return fallback
