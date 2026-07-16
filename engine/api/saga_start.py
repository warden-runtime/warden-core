"""
Start-saga handler: creates a saga instance and steps from a definition, then emits
SAGA_STARTED so the engine consumer runs the state machine. All in one transaction.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import yaml
from common.config import get_settings
from common.contracts import SagaEventPayload
from common.models import (
    EventType,
    SagaDefinition,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
)
from common.outbox import emit_saga_event
from common.plugins.registry import get_registry
from common.saga_assets import (
    load_compensation_definition,
    load_output_schema,
    validate_compensation_definition_dict,
)
from common.schemas.saga import ReasonSagaStep, ResourcesSpec, SagaStep, ToolsSpec
from common.topics import TOPIC_ORCHESTRATOR_EVENTS
from pydantic import TypeAdapter
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.transactions import in_transaction

logger = logging.getLogger(__name__)

_DEFINITION_NOT_FOUND = (
    "SagaDefinition not found: namespace={namespace!r}, name={name!r}, version={version!r}"
)
_SAGA_STEP_ADAPTER = TypeAdapter(SagaStep)


@dataclass(frozen=True)
class _ResolvedStepAssets:
    resolved_output_schema: dict[str, Any] | None
    compensation_definition: dict[str, Any] | None


async def _resolve_step_assets(
    steps_body: list[Any],
    *,
    schemas_root: str | None,
    compensations_root: str | None,
) -> list[_ResolvedStepAssets]:
    """Load output_schema and compensation files for saga start."""
    resolved: list[_ResolvedStepAssets] = []
    for order_index, step_spec in enumerate(steps_body):
        if not isinstance(step_spec, dict):
            raise ValueError(f"Saga step at index {order_index} must be a mapping")
        step_model = _SAGA_STEP_ADAPTER.validate_python(step_spec)
        step_id = step_model.id
        try:
            resolved_output_schema = await load_output_schema(
                schemas_root=schemas_root,
                ref=step_model.output_schema,
            )
            embedded_comp = step_model.compensation_definition
            if embedded_comp is None and isinstance(step_spec, dict):
                raw_embedded = step_spec.get("compensation_definition")
                if isinstance(raw_embedded, dict):
                    embedded_comp = raw_embedded
            if embedded_comp is not None:
                compensation_definition = validate_compensation_definition_dict(embedded_comp)
            else:
                compensation_definition = await load_compensation_definition(
                    compensations_root=compensations_root,
                    ref=step_model.compensation,
                )
        except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as e:
            raise ValueError(
                f"Saga step {order_index} (id={step_id!r}) output_schema={step_model.output_schema!r} "
                f"or compensation={step_model.compensation!r}: {e}"
            ) from e
        resolved.append(
            _ResolvedStepAssets(
                resolved_output_schema=resolved_output_schema,
                compensation_definition=compensation_definition,
            )
        )
    return resolved


async def _existing_start_trace_id(
    *,
    namespace: str,
    idempotency_key: str | None,
    conn: BaseDBAsyncClient | None = None,
) -> str | None:
    if idempotency_key is None:
        return None
    q = SagaInstance.filter(namespace=namespace, start_idempotency_key=idempotency_key)
    if conn is not None:
        q = q.using_db(conn)
    existing = await q.first()
    if existing is None:
        return None
    logger.info(
        "Idempotent start: returning existing saga %s for key %s",
        existing.trace_id,
        idempotency_key,
    )
    return existing.trace_id


async def _require_saga_definition(
    *,
    namespace: str,
    name: str,
    version: str,
    conn: BaseDBAsyncClient | None = None,
) -> SagaDefinition:
    q = SagaDefinition.filter(namespace=namespace, name=name, version=version)
    if conn is not None:
        q = q.using_db(conn)
    definition = await q.first()
    if definition is None:
        raise ValueError(
            _DEFINITION_NOT_FOUND.format(namespace=namespace, name=name, version=version)
        )
    return definition


async def _create_saga_and_steps(
    *,
    conn: BaseDBAsyncClient,
    definition: SagaDefinition,
    namespace: str,
    name: str,
    version: str,
    input: dict[str, Any],
    idempotency_key: str | None,
    resolved_assets: list[_ResolvedStepAssets],
) -> str:
    txn_steps_body = definition.body.get("steps") or []
    if len(txn_steps_body) != len(resolved_assets):
        raise ValueError(
            f"SagaDefinition {name!r} v{version!r} changed during start; retry the request."
        )

    trace_id = uuid.uuid4().hex
    step_shells: dict[str, dict[str, Any]] = {}
    for step_spec in txn_steps_body:
        if not isinstance(step_spec, dict):
            raise ValueError("Saga step must be a mapping")
        shell_model = _SAGA_STEP_ADAPTER.validate_python(step_spec)
        step_shells[shell_model.id] = {"output": {"data": {}}, "facts": {}}
    context = {"input": input or {}, "steps": step_shells}
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace=namespace,
        definition_id=str(definition.id),
        status=SagaStatus.PENDING,
        context=context,
        start_idempotency_key=idempotency_key,
        using_db=conn,
    )
    await get_registry().engine.on_saga_created(
        saga=saga,
        conn=conn,
        definition_id=str(definition.id),
        definition_name=definition.name,
        definition_version=definition.version,
        step_count=len(txn_steps_body),
    )
    for order_index, step_spec in enumerate(txn_steps_body):
        if not isinstance(step_spec, dict):
            raise ValueError(f"Saga step at index {order_index} must be a mapping")
        step_model = _SAGA_STEP_ADAPTER.validate_python(step_spec)
        step_id = step_model.id
        step_name = step_model.name
        assets = resolved_assets[order_index]
        tools_spec = step_model.tools or ToolsSpec()
        tool_specs: list[dict[str, Any]] = [t.model_dump(mode="json") for t in tools_spec.allow]
        resources_spec = step_model.resources or ResourcesSpec()
        resource_specs: list[dict[str, Any]] = [
            r.model_dump(mode="json") for r in resources_spec.allow
        ]
        prompt_ref = step_model.prompt if step_model.kind == "reason" else None
        facts_extractors = (
            [f.model_dump() for f in step_model.facts]
            if isinstance(step_model, ReasonSagaStep) and step_model.facts
            else []
        )
        span_id = uuid.uuid4().hex[:16]
        step = await SagaStepInstance.create(
            span_id=span_id,
            saga_trace_id=trace_id,
            namespace=namespace,
            saga=saga,
            step_id=step_id,
            step_name=step_name,
            order_index=order_index,
            idempotency_key=f"{trace_id}-{step_id}",
            timeout_seconds=step_model.timeout_seconds,
            max_turns=step_model.max_turns,
            max_step_tokens=(
                step_model.max_step_tokens if isinstance(step_model, ReasonSagaStep) else None
            ),
            agent_adapter=(
                step_model.agent_adapter if isinstance(step_model, ReasonSagaStep) else "react"
            ),
            status=StepStatus.PENDING,
            worker=step_model.worker,
            worker_version=step_model.worker_version,
            step_kind=step_model.kind,
            tools_allow=tool_specs,
            resources_allow=resource_specs,
            parameters_spec=step_spec.get("with") or {},
            resolved_arguments={},
            prompt_ref=prompt_ref,
            output_payload=None,
            error_details=None,
            compensation_definition=assets.compensation_definition,
            output_schema=assets.resolved_output_schema,
            policy_name=step_model.policy,
            hitl_required=step_model.hitl,
            hitl_max_retries=step_model.hitl_max_retries if step_model.hitl else None,
            hitl_retry_count=0,
            hitl_retry_guidance=step_model.hitl_retry_guidance if step_model.hitl else None,
            pending_review_payload=None,
            when_cel=step_model.when.cel if step_model.when else None,
            facts_extractors=facts_extractors,
            using_db=conn,
        )
        await get_registry().engine.on_step_created(saga=saga, step=step, conn=conn)

    payload = SagaEventPayload(
        namespace=namespace,
        saga_trace_id=trace_id,
        step_span_id=None,
        status="PENDING",
        output={},
    )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type=EventType.SAGA_STARTED.value,
        payload_schema=payload,
        conn=conn,
    )
    return trace_id


async def start_saga(
    *,
    namespace: str,
    name: str,
    version: str,
    input: dict[str, Any],
    idempotency_key: str | None = None,
) -> str:
    """Create saga and step instances from definition, emit SAGA_STARTED in one transaction.

    When idempotency_key is provided, if a saga was already started with that key
    in this namespace, returns its trace_id without creating a new saga.

    Args:
        namespace: Tenant namespace.
        name: Saga definition name.
        version: Saga definition version.
        input: Initial context input (stored in saga.context["input"]).
        idempotency_key: Optional client key; when set, duplicate starts return existing trace_id.

    Returns:
        Saga trace_id (32-char hex), either newly created or existing when idempotency_key matched.

    Raises:
        ValueError: If no SagaDefinition exists for (namespace, name, version).
    """
    existing_trace_id = await _existing_start_trace_id(
        namespace=namespace,
        idempotency_key=idempotency_key,
    )
    if existing_trace_id is not None:
        return existing_trace_id

    definition = await _require_saga_definition(namespace=namespace, name=name, version=version)
    settings = get_settings()
    steps_body = definition.body.get("steps") or []
    resolved_assets = await _resolve_step_assets(
        steps_body,
        schemas_root=settings.schemas_root,
        compensations_root=settings.compensations_root,
    )

    async with in_transaction() as conn:
        existing_trace_id = await _existing_start_trace_id(
            namespace=namespace,
            idempotency_key=idempotency_key,
            conn=conn,
        )
        if existing_trace_id is not None:
            return existing_trace_id

        definition = await _require_saga_definition(
            namespace=namespace,
            name=name,
            version=version,
            conn=conn,
        )
        trace_id = await _create_saga_and_steps(
            conn=conn,
            definition=definition,
            namespace=namespace,
            name=name,
            version=version,
            input=input,
            idempotency_key=idempotency_key,
            resolved_assets=resolved_assets,
        )

    logger.info("Started saga %s (namespace=%s, definition=%s)", trace_id, namespace, name)
    return trace_id
