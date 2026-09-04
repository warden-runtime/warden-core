import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import yaml
from common.config import get_settings
from common.loops import validate_until_cel_compile
from common.manifest_validation import manifest_validation_error
from common.models import SagaDefinition, StepDefinition, WorkerDefinition
from common.plugins.registry import get_registry
from common.policy.cel_eval import PolicyEvaluationError, compile_cel_program
from common.policy.loader import load_policy_artifact_with_meta
from common.saga_assets import (
    assert_output_schema_readable,
    load_compensation_definition,
)
from common.schemas.saga import (
    HydratedSagaBlueprint,
    JoinSagasStep,
    LoopSagaStep,
    ReasonSagaStep,
    SagaAuthoringBlueprint,
    SagaStep,
    SpawnSagasStep,
)
from common.schemas.step import (
    CommitStepBlueprint,
    ReasonStepBlueprint,
    parse_step_blueprint,
)
from common.schemas.worker import WorkerBlueprint
from common.step_facts import validate_facts_extractors
from common.step_hydrate import hydrate_authoring_blueprint
from common.step_when import validate_when_cel_compile
from common.worker_ref import WorkerIdentity, resolve_worker_from_compensation
from pydantic import BaseModel, ValidationError
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from engine.saga_hydrate import (
    load_catalog_for_authoring,
)
from engine.utils import assert_reason_step_prompt

logger = logging.getLogger(__name__)


def _definition_body_payload(blueprint: BaseModel) -> dict[str, Any]:
    """Round-trip a validated blueprint through JSON for JSONField-safe storage.

    ``by_alias=True`` keeps stored keys aligned with YAML manifest names.
    """
    return json.loads(blueprint.model_dump_json(by_alias=True, exclude_none=True))


def _immutable_version_error(*, kind: str, name: str, version: str) -> ValueError:
    label = "Step capability" if kind == "step" else "Worker"
    return ValueError(
        f"{label} '{name}' version '{version}' already exists and is immutable. "
        "Bump the version in your manifest to deploy changes."
    )


async def _create_worker_definition(
    *,
    blueprint: WorkerBlueprint,
    body_payload: dict[str, Any],
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
        raise _immutable_version_error(
            kind="worker", name=blueprint.name, version=blueprint.version
        )

    try:
        await WorkerDefinition.create(body=body_payload, using_db=conn, **lookup)
    except IntegrityError as e:
        raise _immutable_version_error(
            kind="worker", name=blueprint.name, version=blueprint.version
        ) from e


async def _upsert_saga_definition(
    *,
    authoring: SagaAuthoringBlueprint,
    body_payload: dict[str, Any],
    conn: BaseDBAsyncClient,
) -> None:
    lookup = {
        "namespace": authoring.namespace,
        "name": authoring.name,
        "version": authoring.version,
    }

    async def _locked_row() -> SagaDefinition | None:
        return await SagaDefinition.filter(**lookup).using_db(conn).select_for_update().first()

    existing = await _locked_row()
    if existing is not None:
        existing.body = body_payload
        existing.updated_at = datetime.now(UTC)
        await existing.save(using_db=conn, update_fields=["body", "updated_at"])
        return

    try:
        await SagaDefinition.create(body=body_payload, using_db=conn, **lookup)
    except IntegrityError:
        existing = await _locked_row()
        if existing is None:
            raise
        existing.body = body_payload
        existing.updated_at = datetime.now(UTC)
        await existing.save(using_db=conn, update_fields=["body", "updated_at"])


async def _create_step_definition(
    *,
    blueprint: ReasonStepBlueprint | CommitStepBlueprint,
    body_payload: dict[str, Any],
    conn: BaseDBAsyncClient,
) -> None:
    lookup = {
        "namespace": blueprint.namespace,
        "name": blueprint.name,
        "version": blueprint.version,
    }

    async def _locked_row() -> StepDefinition | None:
        return await StepDefinition.filter(**lookup).using_db(conn).select_for_update().first()

    existing = await _locked_row()
    if existing is not None:
        raise _immutable_version_error(kind="step", name=blueprint.name, version=blueprint.version)

    try:
        await StepDefinition.create(body=body_payload, using_db=conn, **lookup)
    except IntegrityError as e:
        raise _immutable_version_error(
            kind="step", name=blueprint.name, version=blueprint.version
        ) from e


async def _validate_step_assets(
    *,
    schemas_root: str | None,
    step: SagaStep | ReasonStepBlueprint | CommitStepBlueprint,
) -> None:
    await assert_output_schema_readable(
        schemas_root=schemas_root,
        ref=step.output_schema,
    )


async def _validate_step_compensation(
    *,
    compensations_root: str | None,
    step: SagaStep | ReasonStepBlueprint | CommitStepBlueprint,
) -> dict[str, Any] | None:
    return await load_compensation_definition(
        compensations_root=compensations_root,
        ref=step.compensation,
    )


async def _validate_reason_prompt(
    *,
    prompts_root: str | None,
    prompt_ref: str,
    param_keys: set[str],
    label: str,
) -> None:
    try:
        await assert_reason_step_prompt(
            prompts_root=prompts_root,
            prompt_ref=prompt_ref,
            param_keys=param_keys,
        )
    except (OSError, ValueError) as e:
        raise ValueError(f"{label} prompt is invalid: {e}") from e


async def _validate_step_skills(
    *,
    skills_root: str | None,
    worker: str,
    skills_allow: list[Any],
    label: str,
) -> None:
    if not skills_allow:
        return
    from common.skills import SkillLoadError, validate_skill_files_at_register

    try:
        validate_skill_files_at_register(
            skills_root,
            worker,
            [s.name for s in skills_allow],
        )
    except SkillLoadError as e:
        raise ValueError(f"{label} skills are invalid: {e.message}") from e
    except (OSError, ValueError) as e:
        raise ValueError(f"{label} skills are invalid: {e}") from e


async def _validate_step_policy(
    *,
    policies_root: str | None,
    policy_ref: str | None,
    label: str,
    legacy_policy_warned: set[str],
) -> None:
    ref = (policy_ref or "").strip()
    if not ref:
        return
    try:
        artifact, used_legacy = await load_policy_artifact_with_meta(
            policies_root=policies_root,
            policy_ref=ref,
        )
        compile_cel_program(artifact.cel_source)
    except (PolicyEvaluationError, OSError, ValueError, FileNotFoundError) as e:
        raise ValueError(f"{label} policy is invalid: {e}") from e
    if used_legacy and ref not in legacy_policy_warned:
        legacy_policy_warned.add(ref)
        logger.warning(
            "policy ref %r resolved via legacy .yaml suffix; "
            "use an explicit path (e.g. %r.yaml) in the step or saga manifest",
            ref,
            ref,
        )


async def _validate_one_saga_step_at_registration(
    *,
    index: int | str,
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
    if isinstance(step, ReasonSagaStep):
        await _validate_reason_prompt(
            prompts_root=settings.prompts_root,
            prompt_ref=step.prompt,
            param_keys=set(step.with_spec.keys()),
            label=step_label,
        )
        await _validate_step_skills(
            skills_root=settings.skills_root,
            worker=step.worker,
            skills_allow=step.skills.allow if step.skills else [],
            label=step_label,
        )
    await _validate_step_policy(
        policies_root=settings.policies_root,
        policy_ref=step.policy,
        label=step_label,
        legacy_policy_warned=legacy_policy_warned,
    )
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


async def _validate_step_blueprint_at_registration(
    *,
    blueprint: ReasonStepBlueprint | CommitStepBlueprint,
    settings: Any,
) -> dict[str, Any] | None:
    """Validate on-disk artifacts for a catalog step (commit invariants already on blueprint)."""
    label = f"Step {blueprint.name!r}@{blueprint.version}"
    legacy_policy_warned: set[str] = set()
    try:
        await _validate_step_assets(
            schemas_root=settings.schemas_root,
            step=blueprint,
        )
        comp_d = await _validate_step_compensation(
            compensations_root=settings.compensations_root,
            step=blueprint,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"{label} output_schema or compensation is invalid: {e}") from e
    if isinstance(blueprint, ReasonStepBlueprint):
        await _validate_reason_prompt(
            prompts_root=settings.prompts_root,
            prompt_ref=blueprint.prompt,
            param_keys=set(blueprint.inputs.keys()),
            label=label,
        )
        await _validate_step_skills(
            skills_root=settings.skills_root,
            worker=blueprint.worker,
            skills_allow=blueprint.skills.allow if blueprint.skills else [],
            label=label,
        )
        if blueprint.facts:
            try:
                validate_facts_extractors(blueprint.facts)
            except ValueError as e:
                raise ValueError(f"{label} facts extractors are invalid: {e}") from e
    await _validate_step_policy(
        policies_root=settings.policies_root,
        policy_ref=blueprint.policy,
        label=label,
        legacy_policy_warned=legacy_policy_warned,
    )
    from common.step_input_schema import validate_step_blueprint_input_schemas

    try:
        validate_step_blueprint_input_schemas(blueprint.inputs)
    except ValueError as e:
        raise ValueError(f"{label} inputs are invalid: {e}") from e
    return comp_d


async def _assert_child_saga_definitions_registered(blueprint: HydratedSagaBlueprint) -> None:
    """Ensure every spawn_sagas target definition exists and is active in the parent namespace."""
    from common.catalog_errors import (
        CatalogDefinitionNotFoundError,
        InactiveCatalogDefinitionError,
    )

    missing: list[str] = []
    inactive: list[str] = []
    for step in blueprint.steps:
        if not isinstance(step, SpawnSagasStep):
            continue
        found = await SagaDefinition.filter(
            namespace=blueprint.namespace,
            name=step.spawn.saga_name,
            version=step.spawn.saga_version,
        ).first()
        label = f"{step.spawn.saga_name}@{step.spawn.saga_version}"
        if found is None:
            missing.append(label)
        elif not found.is_active:
            inactive.append(label)
    if missing:
        label = sorted(set(missing))[0]
        name, version = label.split("@", 1)
        raise CatalogDefinitionNotFoundError(
            kind="saga",
            namespace=blueprint.namespace,
            name=name,
            version=version,
            message=(
                "This saga spawns child definitions that are not registered. "
                "Please register these saga manifests first: "
                f"{sorted(set(missing))}"
            ),
        )
    if inactive:
        label = sorted(set(inactive))[0]
        name, version = label.split("@", 1)
        raise InactiveCatalogDefinitionError(
            kind="saga",
            namespace=blueprint.namespace,
            name=name,
            version=version,
            message=(
                "This saga spawns child definitions that are inactive. "
                "Re-activate them or pin an active version: "
                f"{sorted(set(inactive))}"
            ),
        )


async def _collect_saga_registration_workers(
    blueprint: HydratedSagaBlueprint,
    settings: Any,
) -> set[WorkerIdentity]:
    required_workers: set[WorkerIdentity] = set()
    legacy_policy_warned: set[str] = set()

    async def _register_executable(index_label: str, step: SagaStep) -> None:
        required_workers.add((step.worker, step.worker_version))
        comp_d = await _validate_one_saga_step_at_registration(
            index=index_label,
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

    for i, step in enumerate(blueprint.steps):
        if isinstance(step, LoopSagaStep):
            try:
                validate_until_cel_compile(step.until.cel)
            except PolicyEvaluationError as e:
                raise ValueError(
                    f"Saga step {i} (loop id={step.id!r}) until.cel is invalid: {e}"
                ) from e
            for j, body in enumerate(step.steps):
                await _register_executable(f"{i}.body[{j}]", body)
            continue
        if isinstance(step, (SpawnSagasStep, JoinSagasStep)):
            step_label = f"Saga step {i} (id={step.id!r})"
            if step.when is not None:
                try:
                    validate_when_cel_compile(step.when.cel)
                except PolicyEvaluationError as e:
                    raise ValueError(f"{step_label} when.cel is invalid: {e}") from e
            continue
        await _register_executable(str(i), step)
    return required_workers


async def _assert_saga_workers_registered(
    blueprint: HydratedSagaBlueprint,
    required_workers: set[WorkerIdentity],
) -> None:
    from common.catalog_errors import (
        CatalogDefinitionNotFoundError,
        InactiveCatalogDefinitionError,
    )

    if not required_workers:
        return
    rows = await WorkerDefinition.filter(namespace=blueprint.namespace).all()
    by_key = {(row.name, row.version): row for row in rows}
    missing: list[str] = []
    inactive: list[str] = []
    for name, version in sorted(required_workers):
        label = f"{name}@{version}"
        found = by_key.get((name, version))
        if found is None:
            missing.append(label)
        elif not found.is_active:
            inactive.append(label)
    if missing:
        label = missing[0]
        name, version = label.split("@", 1)
        raise CatalogDefinitionNotFoundError(
            kind="worker",
            namespace=blueprint.namespace,
            name=name,
            version=version,
            message=(
                "This saga requires workers that are not registered. "
                "Please register these worker manifests first: "
                f"{missing}"
            ),
        )
    if inactive:
        label = inactive[0]
        name, version = label.split("@", 1)
        raise InactiveCatalogDefinitionError(
            kind="worker",
            namespace=blueprint.namespace,
            name=name,
            version=version,
            message=(
                "This saga requires workers that are inactive. "
                "Re-activate them or pin an active version: "
                f"{inactive}"
            ),
        )


async def _assert_step_worker_registered(
    blueprint: ReasonStepBlueprint | CommitStepBlueprint,
) -> None:
    from common.catalog_errors import (
        CatalogDefinitionNotFoundError,
        InactiveCatalogDefinitionError,
    )

    found = await WorkerDefinition.filter(
        namespace=blueprint.namespace,
        name=blueprint.worker,
        version=blueprint.worker_version,
    ).first()
    if found is None:
        raise CatalogDefinitionNotFoundError(
            kind="worker",
            namespace=blueprint.namespace,
            name=blueprint.worker,
            version=blueprint.worker_version,
            message=(
                "This step requires a worker that is not registered. "
                "Please register this worker manifest first: "
                f"{blueprint.worker}@{blueprint.worker_version}"
            ),
        )
    if not found.is_active:
        raise InactiveCatalogDefinitionError(
            kind="worker",
            namespace=blueprint.namespace,
            name=blueprint.worker,
            version=blueprint.worker_version,
        )


class RegistryService:
    """Registers worker, step, and saga manifests (YAML) into the database."""

    async def register_manifest(self, yaml_content: str) -> str:
        """Parse YAML manifest, validate against blueprint, and persist to DB.

        Args:
            yaml_content: Raw YAML string (must have kind: worker | step | saga and
                kind-specific fields).

        Returns:
            Human-readable success message (e.g. "Worker 'x' registered successfully").

        Raises:
            ValueError: Invalid YAML, non-dict root, unknown kind, or missing deps.
        """
        try:
            data = await asyncio.to_thread(yaml.safe_load, yaml_content)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format: {e}") from e

        return await self.register_manifest_from_dict(data)

    async def register_manifest_from_dict(self, data: dict[str, Any]) -> str:
        """Validate manifest dict and persist to DB. Used by API and CLI.

        Args:
            data: Parsed manifest (must have kind: worker | step | saga and kind-specific fields).

        Returns:
            Human-readable success message.

        Raises:
            ValueError: Non-dict, unknown kind, schema validation failure, or missing deps.
        """
        if not isinstance(data, dict):
            raise ValueError("Invalid manifest: root must be a mapping (e.g. kind, name, ...).")

        kind = data.get("kind")
        if kind == "worker":
            return await self._register_worker(data)
        if kind == "step":
            return await self._register_step(data)
        if kind == "saga":
            return await self._register_saga(data)
        raise ValueError(f"Unknown manifest kind: {kind!r}.")

    async def _register_worker(self, data: dict[str, Any]) -> str:
        try:
            blueprint = WorkerBlueprint(**data)
        except ValidationError as exc:
            raise manifest_validation_error(exc) from exc
        body_payload = _definition_body_payload(blueprint)

        async with in_transaction() as conn:
            await _create_worker_definition(
                blueprint=blueprint,
                body_payload=body_payload,
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

    async def _register_step(self, data: dict[str, Any]) -> str:
        try:
            blueprint = parse_step_blueprint(data)
        except ValidationError as exc:
            raise manifest_validation_error(exc) from exc

        settings = get_settings()
        await _assert_step_worker_registered(blueprint)
        await _validate_step_blueprint_at_registration(blueprint=blueprint, settings=settings)
        body_payload = _definition_body_payload(blueprint)

        async with in_transaction() as conn:
            await _create_step_definition(
                blueprint=blueprint,
                body_payload=body_payload,
                conn=conn,
            )
            await get_registry().engine.on_manifest_registered(
                kind="step",
                blueprint=blueprint,
                conn=conn,
            )

        logger.debug(
            "registered step name=%s namespace=%s version=%s step_kind=%s",
            blueprint.name,
            blueprint.namespace,
            blueprint.version,
            blueprint.step_kind,
        )
        return f"Step '{blueprint.name}' v{blueprint.version} registered successfully"

    async def _register_saga(self, data: dict[str, Any]) -> str:
        try:
            authoring = SagaAuthoringBlueprint(**data)
        except ValidationError as exc:
            raise manifest_validation_error(exc) from exc

        loaded = await load_catalog_for_authoring(
            authoring=authoring,
            namespace=authoring.namespace,
        )

        def _resolve(name: str, version: str) -> ReasonStepBlueprint | CommitStepBlueprint:
            return loaded[(name, version)]

        try:
            # Transient hydrate for link-check only — not persisted on the definition.
            hydrated = hydrate_authoring_blueprint(authoring, resolve_step=_resolve)
        except ValidationError as exc:
            raise manifest_validation_error(exc) from exc
        except ValueError as e:
            raise ValueError(str(e)) from e

        settings = get_settings()
        required_workers = await _collect_saga_registration_workers(hydrated, settings)
        await _assert_saga_workers_registered(hydrated, required_workers)
        await _assert_child_saga_definitions_registered(hydrated)

        body_payload = _definition_body_payload(authoring)

        async with in_transaction() as conn:
            await _upsert_saga_definition(
                authoring=authoring,
                body_payload=body_payload,
                conn=conn,
            )
            await get_registry().engine.on_manifest_registered(
                kind="saga",
                blueprint=authoring,
                conn=conn,
            )

        logger.debug(
            "registered saga name=%s namespace=%s version=%s",
            authoring.name,
            authoring.namespace,
            authoring.version,
        )
        return f"Saga '{authoring.name}' v{authoring.version} registered successfully"
