"""Create SagaStepInstance rows from executable step specs (start + loop mint)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from common.loops import forward_idempotency_key, parse_executable_step
from common.models import SagaInstance, SagaStepInstance, StepStatus
from common.plugins.registry import get_registry
from common.schemas.saga import (
    DEFAULT_MAX_TURNS,
    ENGINE_NATIVE_WORKER,
    ENGINE_NATIVE_WORKER_VERSION,
    CommitSagaStep,
    JoinSagasStep,
    ReasonSagaStep,
    ResourcesSpec,
    SkillsSpec,
    SpawnSagasStep,
    ToolsSpec,
)

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient


def _assets_for_index(
    step_spec: dict[str, Any],
    index: int,
    resolved_assets: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if resolved_assets is not None and index < len(resolved_assets):
        return resolved_assets[index]
    schema = step_spec.get("output_schema_resolved")
    comp = step_spec.get("compensation_definition")
    return (
        schema if isinstance(schema, dict) else None,
        comp if isinstance(comp, dict) else None,
    )


def _reason_fields(step_model: ReasonSagaStep | Any) -> dict[str, Any]:
    if not isinstance(step_model, ReasonSagaStep):
        return {
            "max_step_tokens": None,
            "max_completion_tokens": None,
            "agent_adapter": "react",
            "prompt_ref": None,
            "facts_extractors": [],
        }
    return {
        "max_step_tokens": step_model.max_step_tokens,
        "max_completion_tokens": step_model.max_completion_tokens,
        "agent_adapter": step_model.agent_adapter,
        "prompt_ref": step_model.prompt,
        "facts_extractors": (
            [f.model_dump() for f in step_model.facts] if step_model.facts else []
        ),
    }


def _step_create_fields(
    *,
    saga: SagaInstance,
    step_spec: dict[str, Any],
    forward_seq: int,
    order_index: int,
    resolved_output_schema: dict[str, Any] | None,
    compensation_definition: dict[str, Any] | None,
) -> dict[str, Any]:
    clean = {k: v for k, v in step_spec.items() if not str(k).startswith("_")}
    loop_id = step_spec.get("_loop_id")
    iteration = step_spec.get("_loop_iteration")
    step_model = parse_executable_step(clean)
    loop_id_str = str(loop_id) if loop_id else None
    iteration_int = int(iteration) if iteration is not None else None
    base = {
        "span_id": uuid.uuid4().hex[:16],
        "saga_trace_id": saga.trace_id,
        "namespace": saga.namespace,
        "saga": saga,
        "step_id": step_model.id,
        "step_name": step_model.name,
        "order_index": order_index,
        "forward_seq": forward_seq,
        "loop_id": loop_id_str,
        "iteration": iteration_int,
        "idempotency_key": forward_idempotency_key(
            trace_id=saga.trace_id,
            step_id=step_model.id,
            loop_id=loop_id_str,
            iteration=iteration_int,
        ),
        "timeout_seconds": step_model.timeout_seconds,
        "status": StepStatus.PENDING,
        "step_kind": step_model.kind,
        "resolved_arguments": {},
        "output_payload": None,
        "error_details": None,
        "compensation_definition": compensation_definition,
        "output_schema": resolved_output_schema,
        "hitl_retry_count": 0,
        "pending_review_payload": None,
        "when_cel": step_model.when.cel if step_model.when else None,
    }
    if isinstance(step_model, (SpawnSagasStep, JoinSagasStep)):
        base.update(
            {
                "max_turns": DEFAULT_MAX_TURNS,
                "worker": ENGINE_NATIVE_WORKER,
                "worker_version": ENGINE_NATIVE_WORKER_VERSION,
                "tools_allow": [],
                "resources_allow": [],
                "skills_allow": [],
                "parameters_spec": {},
                "policy_name": None,
                "hitl_required": False,
                "hitl_max_retries": None,
                "hitl_retry_guidance": None,
            }
        )
        base.update(_reason_fields(None))
        return base

    if not isinstance(step_model, (ReasonSagaStep, CommitSagaStep)):
        raise ValueError(f"Unsupported step kind for materialization: {step_model.kind!r}")

    tools_spec = step_model.tools or ToolsSpec()
    resources_spec = step_model.resources or ResourcesSpec()
    skills_spec = (
        step_model.skills
        if isinstance(step_model, ReasonSagaStep) and step_model.skills is not None
        else SkillsSpec()
    )
    base.update(
        {
            "max_turns": step_model.max_turns,
            "worker": step_model.worker,
            "worker_version": step_model.worker_version,
            "tools_allow": [t.model_dump(mode="json") for t in tools_spec.allow],
            "resources_allow": [r.model_dump(mode="json") for r in resources_spec.allow],
            "skills_allow": [s.model_dump(mode="json") for s in skills_spec.allow],
            "parameters_spec": clean.get("with") or {},
            "policy_name": step_model.policy,
            "hitl_required": step_model.hitl,
            "hitl_max_retries": step_model.hitl_max_retries if step_model.hitl else None,
            "hitl_retry_guidance": step_model.hitl_retry_guidance if step_model.hitl else None,
        }
    )
    base.update(_reason_fields(step_model))
    return base


async def materialize_executable_steps(
    *,
    saga: SagaInstance,
    step_specs: list[dict[str, Any]],
    start_forward_seq: int,
    start_order_index: int,
    conn: BaseDBAsyncClient,
    resolved_assets: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] | None = None,
) -> list[SagaStepInstance]:
    """Persist forward step rows for ``step_specs`` with contiguous forward_seq.

    ``step_specs`` may include ``_loop_id`` / ``_loop_iteration`` annotations from
    loop materialization helpers. ``resolved_assets`` is parallel (output_schema,
    compensation_definition); when omitted, values are taken from the step spec.
    """
    created: list[SagaStepInstance] = []
    forward_seq = start_forward_seq
    order_index = start_order_index

    for i, step_spec in enumerate(step_specs):
        schema, comp = _assets_for_index(step_spec, i, resolved_assets)
        fields = _step_create_fields(
            saga=saga,
            step_spec=step_spec,
            forward_seq=forward_seq,
            order_index=order_index,
            resolved_output_schema=schema,
            compensation_definition=comp,
        )
        step = await SagaStepInstance.create(**fields, using_db=conn)
        await get_registry().engine.on_step_created(saga=saga, step=step, conn=conn)
        created.append(step)
        forward_seq += 1
        order_index += 1

    return created
