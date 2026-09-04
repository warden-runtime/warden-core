"""Hydrate stored saga authoring AST into an instance frozen_steps list."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import yaml
from common.catalog_errors import (
    CatalogDefinitionNotFoundError,
    InactiveCatalogDefinitionError,
)
from common.manifest_validation import manifest_validation_error
from common.models import StepDefinition
from common.policy.loader import load_policy_artifact
from common.prompts import freeze_prompt_definition
from common.saga_assets import load_compensation_definition, load_output_schema
from common.schemas.saga import (
    CommitSagaStep,
    ExecutableSagaStep,
    HydratedSagaBlueprint,
    JoinSagasStep,
    LoopAuthoringStep,
    LoopSagaStep,
    ReasonSagaStep,
    SagaAuthoringBlueprint,
    SagaStepRef,
    SpawnSagasStep,
)
from common.schemas.step import CommitStepBlueprint, ReasonStepBlueprint, parse_step_blueprint
from common.skills import (
    SkillLoadError,
    assert_skill_document_complete,
    load_skill_document,
    skill_document_to_definition,
)
from common.step_hydrate import hydrate_authoring_blueprint
from pydantic import ValidationError


async def load_step_blueprint(
    *,
    namespace: str,
    name: str,
    version: str,
) -> ReasonStepBlueprint | CommitStepBlueprint:
    """Load an active step definition blueprint from Postgres."""
    row = await StepDefinition.filter(
        namespace=namespace,
        name=name,
        version=version,
    ).first()
    if row is None:
        raise CatalogDefinitionNotFoundError(
            kind="step",
            namespace=namespace,
            name=name,
            version=version,
            message=(
                f"Step definition {name!r}@{version} is not registered in namespace "
                f"{namespace!r}. Please register this step manifest first."
            ),
        )
    if not row.is_active:
        raise InactiveCatalogDefinitionError(
            kind="step", namespace=namespace, name=name, version=version
        )
    try:
        return parse_step_blueprint(row.body)
    except ValidationError as exc:
        raise manifest_validation_error(exc) from exc


async def _embed_resolved_assets_on_step_dict(
    *,
    step_dict: dict[str, Any],
    step: ExecutableSagaStep,
    compensations_root: str | None,
    policies_root: str | None,
    schemas_root: str | None,
    prompts_root: str | None,
    skills_root: str | None,
) -> None:
    """Freeze compensation, policy, schema, prompt, and skills onto one step dict."""
    if isinstance(step, (SpawnSagasStep, JoinSagasStep)):
        return
    try:
        if not isinstance(step, (ReasonSagaStep, CommitSagaStep)):
            return

        comp = await load_compensation_definition(
            compensations_root=compensations_root,
            ref=step.compensation,
        )
        if comp:
            step_dict["compensation_definition"] = comp

        policy_ref = (step.policy or "").strip()
        if policy_ref:
            artifact = await load_policy_artifact(
                policies_root=policies_root, policy_name=policy_ref
            )
            step_dict["policy_definition"] = {
                "name": artifact.name,
                "version": artifact.version,
                "cel": artifact.cel_source,
            }

        schema = await load_output_schema(
            schemas_root=schemas_root,
            ref=step.output_schema,
        )
        if schema:
            step_dict["output_schema_definition"] = schema

        if isinstance(step, ReasonSagaStep):
            await _embed_reason_prompt_and_skills(
                step_dict=step_dict,
                step=step,
                prompts_root=prompts_root,
                skills_root=skills_root,
            )
    except (
        OSError,
        FileNotFoundError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
        SkillLoadError,
    ) as e:
        step_id = step_dict.get("id") or getattr(step, "id", None)
        raise ValueError(f"Failed to freeze step assets (id={step_id!r}): {e}") from e


async def _freeze_prompt_onto_step_dict(
    *,
    step_dict: dict[str, Any],
    prompt_ref: str,
    prompts_root: str | None,
) -> None:
    if not prompts_root or not str(prompts_root).strip():
        raise ValueError(
            "prompts_root is not configured; set PROMPTS_ROOT to freeze reason-step prompts."
        )
    step_dict["prompt_definition"] = await asyncio.to_thread(
        freeze_prompt_definition, prompts_root, prompt_ref
    )


async def _freeze_skill_docs_for_step(
    *,
    skills_root: str,
    worker_name: str,
    skill_ids: list[str],
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for skill_id in skill_ids:
        doc = await asyncio.to_thread(load_skill_document, skills_root, worker_name, skill_id)
        assert_skill_document_complete(doc, skill_id=skill_id)
        docs.append(skill_document_to_definition(doc))
    return docs


def _skill_ids_from_reason_step(step: ReasonSagaStep) -> list[str]:
    if not step.skills or not step.skills.allow:
        return []
    return [(skill.name or "").strip() for skill in step.skills.allow if (skill.name or "").strip()]


async def _embed_reason_prompt_and_skills(
    *,
    step_dict: dict[str, Any],
    step: ReasonSagaStep,
    prompts_root: str | None,
    skills_root: str | None,
) -> None:
    """Freeze inlined prompt text and skill documents for a reason step."""
    prompt_ref = (step.prompt or "").strip()
    if prompt_ref:
        await _freeze_prompt_onto_step_dict(
            step_dict=step_dict, prompt_ref=prompt_ref, prompts_root=prompts_root
        )
    skill_ids = _skill_ids_from_reason_step(step)
    if not skill_ids:
        return
    if not skills_root or not str(skills_root).strip():
        raise ValueError("skills_root is not configured; set SKILLS_ROOT to freeze skill payloads.")
    docs = await _freeze_skill_docs_for_step(
        skills_root=str(skills_root).strip(),
        worker_name=step.worker,
        skill_ids=skill_ids,
    )
    if docs:
        step_dict["skills_definition"] = docs


def _require_step_dict_at_index(
    steps_body: list[Any],
    index: int,
    *,
    where: str,
) -> dict[str, Any]:
    if index >= len(steps_body):
        raise ValueError(
            f"Hydrated steps body shorter than blueprint at {where} "
            f"(index={index}, body_len={len(steps_body)})"
        )
    entry = steps_body[index]
    if not isinstance(entry, dict):
        raise ValueError(
            f"Hydrated steps body entry at {where} index={index} must be a dict, "
            f"got {type(entry).__name__}"
        )
    return entry


async def embed_resolved_step_assets(
    *,
    blueprint: HydratedSagaBlueprint,
    body_payload: dict[str, Any],
    compensations_root: str | None,
    policies_root: str | None,
    schemas_root: str | None,
    prompts_root: str | None = None,
    skills_root: str | None = None,
) -> None:
    """Walk hydrated steps once and attach frozen disk assets."""
    steps_body = body_payload.get("steps")
    if not isinstance(steps_body, list):
        raise ValueError(
            "Cannot embed step assets: hydrated body 'steps' must be a list "
            f"(got {type(steps_body).__name__})"
        )
    if len(steps_body) != len(blueprint.steps):
        raise ValueError(
            "Cannot embed step assets: blueprint steps length "
            f"({len(blueprint.steps)}) != body steps length ({len(steps_body)})"
        )
    for index, step in enumerate(blueprint.steps):
        step_dict = _require_step_dict_at_index(steps_body, index, where="steps")
        if isinstance(step, LoopSagaStep):
            body_list = step_dict.get("steps")
            if not isinstance(body_list, list):
                raise ValueError(
                    f"Cannot embed loop body assets at steps[{index}]: "
                    f"'steps' must be a list (got {type(body_list).__name__})"
                )
            if len(body_list) != len(step.steps):
                raise ValueError(
                    f"Cannot embed loop body assets at steps[{index}]: "
                    f"blueprint body length ({len(step.steps)}) != "
                    f"dict body length ({len(body_list)})"
                )
            for body_i, body_step in enumerate(step.steps):
                nested = _require_step_dict_at_index(
                    body_list, body_i, where=f"steps[{index}].steps"
                )
                await _embed_resolved_assets_on_step_dict(
                    step_dict=nested,
                    step=body_step,
                    compensations_root=compensations_root,
                    policies_root=policies_root,
                    schemas_root=schemas_root,
                    prompts_root=prompts_root,
                    skills_root=skills_root,
                )
            continue
        await _embed_resolved_assets_on_step_dict(
            step_dict=step_dict,
            step=step,
            compensations_root=compensations_root,
            policies_root=policies_root,
            schemas_root=schemas_root,
            prompts_root=prompts_root,
            skills_root=skills_root,
        )


def collect_authoring_step_refs(authoring: SagaAuthoringBlueprint) -> list[tuple[str, str]]:
    """Unique (name, version) pairs referenced by the authoring graph."""
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in authoring.steps:
        batch: list[SagaStepRef] = []
        if isinstance(node, SagaStepRef):
            batch = [node]
        elif isinstance(node, LoopAuthoringStep):
            batch = list(node.steps)
        for ref in batch:
            key = (ref.use, ref.version)
            if key not in seen:
                seen.add(key)
                refs.append(key)
    return refs


def _authoring_data_from_body(
    *,
    body: dict[str, Any],
    namespace: str,
    name: str,
    version: str,
    description: str,
) -> dict[str, Any]:
    authoring_data = {
        "kind": "saga",
        "name": name,
        "namespace": namespace,
        "version": version,
        "description": description or body.get("description") or "",
        "steps": body.get("steps") if isinstance(body.get("steps"), list) else [],
    }
    for key in ("kind", "name", "namespace", "version", "description", "steps"):
        if key in body and body[key] is not None:
            authoring_data[key] = body[key]
    authoring_data["namespace"] = namespace
    return authoring_data


async def load_catalog_for_authoring(
    *,
    authoring: SagaAuthoringBlueprint,
    namespace: str,
) -> dict[tuple[str, str], ReasonStepBlueprint | CommitStepBlueprint]:
    """Load active step blueprints for every ``use:`` ref in the authoring graph."""
    loaded: dict[tuple[str, str], ReasonStepBlueprint | CommitStepBlueprint] = {}
    missing: list[str] = []
    inactive: list[str] = []
    for step_name, step_version in collect_authoring_step_refs(authoring):
        try:
            loaded[(step_name, step_version)] = await load_step_blueprint(
                namespace=namespace,
                name=step_name,
                version=step_version,
            )
        except CatalogDefinitionNotFoundError:
            missing.append(f"{step_name}@{step_version}")
        except InactiveCatalogDefinitionError:
            inactive.append(f"{step_name}@{step_version}")
    if missing:
        label = sorted(set(missing))[0]
        name, version = label.split("@", 1)
        raise CatalogDefinitionNotFoundError(
            kind="step",
            namespace=namespace,
            name=name,
            version=version,
            message=(
                "This saga requires steps that are not registered. "
                "Please register these step manifests first: "
                f"{sorted(set(missing))}"
            ),
        )
    if inactive:
        label = sorted(set(inactive))[0]
        name, version = label.split("@", 1)
        raise InactiveCatalogDefinitionError(
            kind="step",
            namespace=namespace,
            name=name,
            version=version,
            message=(
                "This saga requires steps that are inactive. "
                "Re-activate them or pin an active version: "
                f"{sorted(set(inactive))}"
            ),
        )
    return loaded


async def hydrate_authoring_body_to_frozen_steps(
    *,
    body: dict[str, Any],
    namespace: str,
    name: str,
    version: str,
    description: str = "",
    compensations_root: str | None,
    policies_root: str | None = None,
    schemas_root: str | None = None,
    prompts_root: str | None = None,
    skills_root: str | None = None,
) -> list[dict[str, Any]]:
    """Parse authoring AST, resolve catalog steps, return executable frozen_steps list."""
    authoring_data = _authoring_data_from_body(
        body=body,
        namespace=namespace,
        name=name,
        version=version,
        description=description,
    )
    try:
        authoring = SagaAuthoringBlueprint.model_validate(authoring_data)
    except ValidationError as exc:
        raise manifest_validation_error(exc) from exc

    loaded = await load_catalog_for_authoring(authoring=authoring, namespace=namespace)

    def _resolve(step_name: str, step_version: str) -> ReasonStepBlueprint | CommitStepBlueprint:
        return loaded[(step_name, step_version)]

    try:
        hydrated = hydrate_authoring_blueprint(authoring, resolve_step=_resolve)
    except ValidationError as exc:
        raise manifest_validation_error(exc) from exc

    payload = json.loads(hydrated.model_dump_json(by_alias=True, exclude_none=True))
    await embed_resolved_step_assets(
        blueprint=hydrated,
        body_payload=payload,
        compensations_root=compensations_root,
        policies_root=policies_root,
        schemas_root=schemas_root,
        prompts_root=prompts_root,
        skills_root=skills_root,
    )
    steps = payload.get("steps")
    if not isinstance(steps, list):
        raise ValueError("Hydrated saga body must contain a steps list")
    return [s for s in steps if isinstance(s, dict)]
