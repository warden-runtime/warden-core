"""Hydrate saga ``use:`` refs against step definitions (link-check + start freeze)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.schemas.saga import (
    CommitSagaStep,
    HydratedSagaBlueprint,
    JoinSagasStep,
    LoopAuthoringStep,
    LoopSagaStep,
    ReasonSagaStep,
    SagaAuthoringBlueprint,
    SagaStepRef,
    SpawnSagasStep,
)
from common.step_tighten import apply_tighten_overrides

if TYPE_CHECKING:
    from collections.abc import Callable

    from common.schemas.saga import StepParameterSpec
    from common.schemas.step import (
        CommitStepBlueprint,
        ReasonStepBlueprint,
        StepInputSpec,
    )

    StepBlueprint = ReasonStepBlueprint | CommitStepBlueprint


def validate_with_against_inputs(
    *,
    step_id: str,
    with_spec: dict[str, StepParameterSpec],
    inputs: dict[str, StepInputSpec],
) -> None:
    """Reject unknown ``with`` keys, missing required ports, and bad ``value:`` literals."""
    provided = set(with_spec.keys())
    declared = set(inputs.keys())
    unknown = sorted(provided - declared)
    if unknown:
        raise ValueError(
            f"step {step_id!r} with keys are not declared inputs on the step definition: {unknown}"
        )
    missing = sorted(key for key, spec in inputs.items() if spec.required and key not in provided)
    if missing:
        raise ValueError(f"step {step_id!r} missing required inputs: {missing}")
    from common.step_input_schema import validate_with_literals_against_input_schemas

    validate_with_literals_against_input_schemas(
        step_id=step_id,
        with_spec=with_spec,
        inputs=inputs,
    )


def hydrate_step_ref(ref: SagaStepRef, step_def: StepBlueprint) -> ReasonSagaStep | CommitSagaStep:
    """Merge a saga ``use:`` node with a step blueprint into a concrete executable step."""
    validate_with_against_inputs(
        step_id=ref.id,
        with_spec=ref.with_spec,
        inputs=step_def.inputs,
    )
    capability = apply_tighten_overrides(
        step_id=ref.id,
        capability=step_def.to_capability_dict(),
        ref=ref,
        step_kind=step_def.step_kind,
    )
    display_name = ref.name.strip() if ref.name and ref.name.strip() else step_def.display_title()
    payload: dict[str, Any] = {
        **capability,
        "id": ref.id,
        "name": display_name,
        "with": {
            key: spec.model_dump(by_alias=True, exclude_none=True)
            for key, spec in ref.with_spec.items()
        },
    }
    if ref.when is not None:
        payload["when"] = ref.when.model_dump(by_alias=True, exclude_none=True)

    if step_def.step_kind == "reason":
        return ReasonSagaStep.model_validate(payload)
    return CommitSagaStep.model_validate(payload)


def _hydrate_authoring_node(
    node: Any,
    *,
    resolve_step: Callable[[str, str], StepBlueprint],
) -> Any:
    if isinstance(node, SagaStepRef):
        return hydrate_step_ref(node, resolve_step(node.use, node.version))
    if isinstance(node, LoopAuthoringStep):
        body = [
            hydrate_step_ref(body_ref, resolve_step(body_ref.use, body_ref.version))
            for body_ref in node.steps
        ]
        return LoopSagaStep(
            kind="loop",
            id=node.id,
            name=node.name,
            max_iterations=node.max_iterations,
            until=node.until,
            steps=body,
        )
    if isinstance(node, (SpawnSagasStep, JoinSagasStep)):
        return node
    raise ValueError(f"Unsupported authoring step: {type(node)!r}")


def hydrate_authoring_blueprint(
    authoring: SagaAuthoringBlueprint,
    *,
    resolve_step: Callable[[str, str], StepBlueprint],
) -> HydratedSagaBlueprint:
    """Resolve all ``use:`` refs via ``resolve_step(name, version) -> step blueprint``.

    Used at saga deploy (transient link-check) and at saga start (instance freeze).
    """
    return HydratedSagaBlueprint(
        kind="saga",
        name=authoring.name,
        namespace=authoring.namespace,
        version=authoring.version,
        description=authoring.description,
        steps=[
            _hydrate_authoring_node(node, resolve_step=resolve_step) for node in authoring.steps
        ],
    )
