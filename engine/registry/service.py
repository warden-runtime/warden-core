import asyncio
import json
import logging
from typing import Any

import yaml
from common.config import get_settings
from common.models import SagaDefinition, WorkerDefinition
from common.plugins.registry import get_registry
from common.policy.cel_eval import PolicyEvaluationError, compile_cel_program
from common.policy.loader import load_policy_artifact_with_meta
from common.saga_assets import (
    assert_output_schema_readable,
    load_compensation_definition,
)
from common.schemas.saga import ReasonSagaStep, SagaBlueprint, SagaStep
from common.schemas.worker import WorkerBlueprint
from common.step_facts import validate_facts_extractors
from common.step_when import validate_when_cel_compile
from common.worker_ref import WorkerIdentity, resolve_worker_from_compensation
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from engine.utils import assert_reason_step_prompt

logger = logging.getLogger(__name__)


def _saga_definition_body_payload(blueprint: SagaBlueprint) -> dict[str, Any]:
    """Round-trip blueprint through JSON for JSONField-safe, alias-resolved storage."""
    return json.loads(blueprint.model_dump_json(by_alias=True, exclude_none=True))


async def _embed_resolved_compensation_definitions(
    *,
    blueprint: SagaBlueprint,
    body_payload: dict[str, Any],
    compensations_root: str | None,
) -> None:
    """Attach frozen compensation blocks to each step in the registry body payload."""
    steps_body = body_payload.get("steps")
    if not isinstance(steps_body, list):
        return
    for index, step in enumerate(blueprint.steps):
        if index >= len(steps_body) or not isinstance(steps_body[index], dict):
            continue
        comp = await _validate_step_compensation(
            compensations_root=compensations_root,
            step=step,
        )
        if comp:
            steps_body[index]["compensation_definition"] = comp


def _worker_definition_fields(blueprint: WorkerBlueprint) -> dict[str, Any]:
    tool_sources = [
        json.loads(source.model_dump_json(by_alias=True, exclude_none=True))
        for source in blueprint.tool_sources
    ]
    return {
        "namespace": blueprint.namespace,
        "version": blueprint.version,
        "model_provider": blueprint.provider,
        "model_name": blueprint.model_name,
        "system_prompt": blueprint.system_prompt,
        "compensation_prompt": blueprint.compensation_prompt,
        "tool_sources": tool_sources,
        "adapter": blueprint.adapter,
    }


async def _upsert_worker_definition(
    *,
    blueprint: WorkerBlueprint,
    fields: dict[str, Any],
    conn: BaseDBAsyncClient,
) -> None:
    lookup = {
        "namespace": blueprint.namespace,
        "name": blueprint.name,
        "version": blueprint.version,
    }

    async def _locked_row() -> WorkerDefinition | None:
        return await WorkerDefinition.filter(**lookup).using_db(conn).select_for_update().first()

    existing = await _locked_row()
    if existing is not None:
        for key, value in fields.items():
            if key in ("namespace", "name", "version"):
                continue
            setattr(existing, key, value)
        await existing.save(using_db=conn)
        return

    try:
        await WorkerDefinition.create(using_db=conn, name=blueprint.name, **fields)
    except IntegrityError:
        existing = await _locked_row()
        if existing is None:
            raise
        for key, value in fields.items():
            if key in ("namespace", "name", "version"):
                continue
            setattr(existing, key, value)
        await existing.save(using_db=conn)


async def _upsert_saga_definition(
    *,
    blueprint: SagaBlueprint,
    body_payload: dict[str, Any],
    conn: BaseDBAsyncClient,
) -> None:
    lookup = {
        "namespace": blueprint.namespace,
        "name": blueprint.name,
        "version": blueprint.version,
    }

    async def _locked_row() -> SagaDefinition | None:
        return await SagaDefinition.filter(**lookup).using_db(conn).select_for_update().first()

    existing = await _locked_row()
    if existing is not None:
        existing.body = body_payload
        await existing.save(using_db=conn, update_fields=["body"])
        return

    try:
        await SagaDefinition.create(body=body_payload, using_db=conn, **lookup)
    except IntegrityError:
        existing = await _locked_row()
        if existing is None:
            raise
        existing.body = body_payload
        await existing.save(using_db=conn, update_fields=["body"])


async def _validate_step_assets(
    *,
    schemas_root: str | None,
    step: SagaStep,
) -> None:
    await assert_output_schema_readable(
        schemas_root=schemas_root,
        ref=step.output_schema,
    )


async def _validate_step_compensation(
    *,
    compensations_root: str | None,
    step: SagaStep,
) -> dict[str, Any] | None:
    return await load_compensation_definition(
        compensations_root=compensations_root,
        ref=step.compensation,
    )


async def _validate_step_prompt(
    *,
    prompts_root: str | None,
    step: SagaStep,
) -> None:
    if not isinstance(step, ReasonSagaStep):
        return
    await assert_reason_step_prompt(
        prompts_root=prompts_root,
        prompt_ref=step.prompt,
        param_keys=set(step.with_spec.keys()),
    )


async def _validate_step_policy(
    *,
    policies_root: str | None,
    step: SagaStep,
    legacy_policy_warned: set[str],
) -> None:
    policy_ref = (step.policy or "").strip()
    if not policy_ref:
        return
    try:
        artifact, used_legacy = await load_policy_artifact_with_meta(
            policies_root=policies_root,
            policy_ref=policy_ref,
        )
        compile_cel_program(artifact.cel_source)
    except PolicyEvaluationError as e:
        raise ValueError(str(e)) from e
    if used_legacy and policy_ref not in legacy_policy_warned:
        legacy_policy_warned.add(policy_ref)
        logger.warning(
            "policy ref %r resolved via legacy .yaml suffix; "
            "use an explicit path (e.g. %r.yaml) in the saga manifest",
            policy_ref,
            policy_ref,
        )


async def _validate_one_saga_step_at_registration(
    *,
    index: int,
    step: SagaStep,
    settings: Any,
    legacy_policy_warned: set[str],
) -> dict[str, Any] | None:
    step_label = f"Saga step {index} (id={step.id!r})"
    try:
        await _validate_step_assets(
            schemas_root=settings.schemas_root,
            step=step,
        )
        comp_d = await _validate_step_compensation(
            compensations_root=settings.compensations_root,
            step=step,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"{step_label} output_schema or compensation is invalid: {e}") from e
    try:
        await _validate_step_prompt(
            prompts_root=settings.prompts_root,
            step=step,
        )
    except (OSError, ValueError) as e:
        raise ValueError(f"{step_label} prompt is invalid: {e}") from e
    try:
        await _validate_step_policy(
            policies_root=settings.policies_root,
            step=step,
            legacy_policy_warned=legacy_policy_warned,
        )
    except (OSError, ValueError, FileNotFoundError) as e:
        raise ValueError(f"{step_label} policy is invalid: {e}") from e
    if step.when is not None:
        try:
            validate_when_cel_compile(step.when.cel)
        except PolicyEvaluationError as e:
            raise ValueError(f"{step_label} when.cel is invalid: {e}") from e
    if isinstance(step, ReasonSagaStep) and step.facts:
        try:
            validate_facts_extractors(step.facts)
        except ValueError as e:
            raise ValueError(f"{step_label} facts extractors are invalid: {e}") from e
    return comp_d


async def _collect_saga_registration_workers(
    blueprint: SagaBlueprint,
    settings: Any,
) -> set[WorkerIdentity]:
    required_workers: set[WorkerIdentity] = {
        (step.worker, step.worker_version) for step in blueprint.steps
    }
    legacy_policy_warned: set[str] = set()
    for i, step in enumerate(blueprint.steps):
        comp_d = await _validate_one_saga_step_at_registration(
            index=i,
            step=step,
            settings=settings,
            legacy_policy_warned=legacy_policy_warned,
        )
        if comp_d:
            required_workers.add(
                resolve_worker_from_compensation(
                    comp_d,
                    forward_worker=step.worker,
                    forward_worker_version=step.worker_version,
                )
            )
    return required_workers


async def _assert_saga_workers_registered(
    blueprint: SagaBlueprint,
    required_workers: set[WorkerIdentity],
) -> None:
    if not required_workers:
        return
    existing_rows = await WorkerDefinition.filter(namespace=blueprint.namespace).values_list(
        "name", "version"
    )
    existing = set(existing_rows)
    missing_workers = sorted(required_workers - existing)
    if missing_workers:
        missing_display = [f"{name}@{version}" for name, version in missing_workers]
        raise ValueError(
            "This saga requires workers that are not registered. "
            "Please register these worker manifests first: "
            f"{missing_display}"
        )


class RegistryService:
    """Registers worker and saga manifests (YAML) into the database."""

    async def register_manifest(self, yaml_content: str) -> str:
        """Parse YAML manifest, validate against blueprint, and persist to DB.

        Args:
            yaml_content: Raw YAML string (must have kind: worker | saga and
                kind-specific fields).

        Returns:
            Human-readable success message (e.g. "Worker 'x' registered successfully").

        Raises:
            ValueError: Invalid YAML, non-dict root, unknown kind, or (for saga)
                missing required workers.
        """
        try:
            data = await asyncio.to_thread(yaml.safe_load, yaml_content)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format: {e}") from e

        return await self.register_manifest_from_dict(data)

    async def register_manifest_from_dict(self, data: dict[str, Any]) -> str:
        """Validate manifest dict and persist to DB. Used by API and CLI.

        Args:
            data: Parsed manifest (must have kind: worker | saga and kind-specific fields).

        Returns:
            Human-readable success message.

        Raises:
            ValueError: Non-dict, unknown kind, or (for saga) missing required workers.
        """
        if not isinstance(data, dict):
            raise ValueError("Invalid manifest: root must be a mapping (e.g. kind, name, ...).")

        kind = data.get("kind")
        if kind == "worker":
            return await self._register_worker(data)
        if kind == "saga":
            return await self._register_saga(data)
        raise ValueError(f"Unknown manifest kind: {kind!r}.")

    async def _register_worker(self, data: dict[str, Any]) -> str:
        blueprint = WorkerBlueprint(**data)
        worker_fields = _worker_definition_fields(blueprint)

        async with in_transaction() as conn:
            await _upsert_worker_definition(
                blueprint=blueprint,
                fields=worker_fields,
                conn=conn,
            )
            await get_registry().engine.on_manifest_registered(
                kind="worker",
                blueprint=blueprint,
                conn=conn,
            )

        logger.debug(
            "registered worker name=%s namespace=%s version=%s",
            blueprint.name,
            blueprint.namespace,
            blueprint.version,
        )
        return f"Worker '{blueprint.name}' registered successfully"

    async def _register_saga(self, data: dict[str, Any]) -> str:
        blueprint = SagaBlueprint(**data)

        settings = get_settings()
        required_workers = await _collect_saga_registration_workers(blueprint, settings)
        await _assert_saga_workers_registered(blueprint, required_workers)

        body_payload = _saga_definition_body_payload(blueprint)
        await _embed_resolved_compensation_definitions(
            blueprint=blueprint,
            body_payload=body_payload,
            compensations_root=settings.compensations_root,
        )

        async with in_transaction() as conn:
            await _upsert_saga_definition(
                blueprint=blueprint,
                body_payload=body_payload,
                conn=conn,
            )
            await get_registry().engine.on_manifest_registered(
                kind="saga",
                blueprint=blueprint,
                conn=conn,
            )

        logger.debug(
            "registered saga name=%s namespace=%s version=%s",
            blueprint.name,
            blueprint.namespace,
            blueprint.version,
        )
        return f"Saga '{blueprint.name}' v{blueprint.version} registered successfully"
