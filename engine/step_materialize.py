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


def _frozen_dict_asset(step_model: Any, clean: dict[str, Any], field: str) -> dict[str, Any] | None:
    embedded = getattr(step_model, field, None)
    if isinstance(embedded, dict):
        return embedded
    raw = clean.get(field)
    return raw if isinstance(raw, dict) else None


def _frozen_list_asset(clean: dict[str, Any], field: str) -> list[Any] | None:
    raw = clean.get(field)
    return raw if isinstance(raw, list) else None


def _frozen_str_asset(clean: dict[str, Any], field: str) -> str | None:
    raw = clean.get(field)
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _reason_fields(
    step_model: ReasonSagaStep | Any,
    *,
    prompt_definition: str | None = None,
    skills_definition: list[Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(step_model, ReasonSagaStep):
        return {
            "max_step_tokens": None,
            "max_completion_tokens": None,
            "agent_adapter": "react",
            "prompt_ref": None,
            "prompt_definition": None,
            "facts_extractors": [],
            "skills_definition": None,
        }
    return {
        "max_step_tokens": step_model.max_step_tokens,
        "max_completion_tokens": step_model.max_completion_tokens,
        "agent_adapter": step_model.agent_adapter,
        "prompt_ref": step_model.prompt,
        "prompt_definition": prompt_definition,
        "facts_extractors": (
            [f.model_dump() for f in step_model.facts] if step_model.facts else []
        ),
        "skills_definition": skills_definition,
    }


def _step_create_fields(
    *,
    saga: SagaInstance,
    step_spec: dict[str, Any],
    forward_seq: int,
    order_index: int,
) -> dict[str, Any]:
    clean = {k: v for k, v in step_spec.items() if not str(k).startswith("_")}
    loop_id = step_spec.get("_loop_id")
    iteration = step_spec.get("_loop_iteration")
    step_model = parse_executable_step(clean)
    loop_id_str = str(loop_id) if loop_id else None
    iteration_int = int(iteration) if iteration is not None else None
    compensation_definition = _frozen_dict_asset(step_model, clean, "compensation_definition")
    output_schema = _frozen_dict_asset(step_model, clean, "output_schema_definition")
    skills_definition = _frozen_list_asset(clean, "skills_definition")
    prompt_definition = _frozen_str_asset(clean, "prompt_definition")
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
        "output_schema": output_schema,
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
                "tools_bind": [],
                "resources_allow": [],
                "skills_allow": [],
                "parameters_spec": {},
                "policy_name": None,
                "policy_definition": None,
                "hitl_required": False,
                "hitl_max_retries": None,
                "hitl_retry_guidance": None,
                "step_definition_name": None,
                "step_definition_version": None,
                "input_ports": {},
            }
        )
        base.update(_reason_fields(None))
        return base

    if not isinstance(step_model, (ReasonSagaStep, CommitSagaStep)):
        raise ValueError(f"Unsupported step kind for materialization: {step_model.kind!r}")

    base["step_definition_name"] = step_model.step_definition_name
    base["step_definition_version"] = step_model.step_definition_version
    base["input_ports"] = dict(step_model.inputs or {})

    tools_spec = step_model.tools or ToolsSpec()
    resources_spec = step_model.resources or ResourcesSpec()
    skills_spec = (
        step_model.skills
        if isinstance(step_model, ReasonSagaStep) and step_model.skills is not None
        else SkillsSpec()
    )
    policy_definition = _frozen_dict_asset(step_model, clean, "policy_definition")
    base.update(
        {
            "max_turns": step_model.max_turns,
            "worker": step_model.worker,
            "worker_version": step_model.worker_version,
            "tools_allow": [t.model_dump(mode="json") for t in tools_spec.allow],
            "tools_bind": list(tools_spec.bind),
            "resources_allow": [r.model_dump(mode="json") for r in resources_spec.allow],
            "skills_allow": [s.model_dump(mode="json") for s in skills_spec.allow],
            "parameters_spec": clean.get("with") or {},
            "policy_name": step_model.policy,
            "policy_definition": policy_definition,
            "hitl_required": step_model.hitl,
            "hitl_max_retries": step_model.hitl_max_retries if step_model.hitl else None,
            "hitl_retry_guidance": step_model.hitl_retry_guidance if step_model.hitl else None,
        }
    )
    base.update(
        _reason_fields(
            step_model,
            prompt_definition=prompt_definition,
            skills_definition=skills_definition,
        )
    )
    return base


async def materialize_executable_steps(
    *,
    saga: SagaInstance,
    step_specs: list[dict[str, Any]],
    start_forward_seq: int,
    start_order_index: int,
    conn: BaseDBAsyncClient,
) -> list[SagaStepInstance]:
    """Persist forward step rows for ``step_specs`` with contiguous forward_seq.

    ``step_specs`` may include ``_loop_id`` / ``_loop_iteration`` annotations from
    loop materialization helpers. Compensation, policy, and output_schema must already
    be embedded on each frozen step dict (start hydrate).
    """
    created: list[SagaStepInstance] = []
    forward_seq = start_forward_seq
    order_index = start_order_index

    for step_spec in step_specs:
        fields = _step_create_fields(
            saga=saga,
            step_spec=step_spec,
            forward_seq=forward_seq,
            order_index=order_index,
        )
        step = await SagaStepInstance.create(**fields, using_db=conn)
        await get_registry().engine.on_step_created(saga=saga, step=step, conn=conn)
        created.append(step)
        forward_seq += 1
        order_index += 1

    return created
