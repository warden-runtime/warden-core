"""
Start-saga handler: creates a saga instance and steps from a definition, then emits
SAGA_STARTED so the engine consumer runs the state machine. All in one transaction.
"""

import json
import logging
import uuid
from typing import Any, NamedTuple

import yaml
from common.config import get_settings
from common.contracts import SagaEventPayload
from common.loops import (
    build_loop_definitions,
    initial_loops_context,
    parse_executable_step,
    take_initial_materialization_segment,
)
from common.models import (
    EventType,
    SagaDefinition,
    SagaInstance,
    SagaStatus,
)
from common.outbox import emit_saga_event
from common.plugins.registry import get_registry
from common.saga_assets import (
    load_compensation_definition,
    load_output_schema,
    validate_compensation_definition_dict,
)
from common.topics import TOPIC_ORCHESTRATOR_EVENTS
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.transactions import in_transaction

from engine.api.saga_errors import (
    DefinitionNotFoundError,
    InactiveSagaDefinitionError,
    StartIdempotencyConflictError,
)
from engine.step_materialize import materialize_executable_steps

logger = logging.getLogger(__name__)

_DEFINITION_NOT_FOUND = (
    "SagaDefinition not found: namespace={namespace!r}, name={name!r}, version={version!r}"
)


class StartSagaResult(NamedTuple):
    trace_id: str
    created: bool


async def resolve_executable_step_assets(
    step_specs: list[dict[str, Any]],
    *,
    schemas_root: str | None,
    compensations_root: str | None,
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Load output_schema and compensation for executable step specs.

    Engine-native spawn/join steps have no schema or compensation artifacts.
    """
    from common.schemas.saga import JoinSagasStep, SpawnSagasStep

    resolved: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    for order_index, step_spec in enumerate(step_specs):
        if not isinstance(step_spec, dict):
            raise ValueError(f"Saga step at index {order_index} must be a mapping")
        clean = {k: v for k, v in step_spec.items() if not str(k).startswith("_")}
        step_model = parse_executable_step(clean)
        if isinstance(step_model, (SpawnSagasStep, JoinSagasStep)):
            resolved.append((None, None))
            continue
        step_id = step_model.id
        try:
            resolved_output_schema = await load_output_schema(
                schemas_root=schemas_root,
                ref=step_model.output_schema,
            )
            embedded_comp = step_model.compensation_definition
            if embedded_comp is None:
                raw_embedded = clean.get("compensation_definition")
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
        resolved.append((resolved_output_schema, compensation_definition))
    return resolved


def _executable_shell_ids(frozen_steps: list[Any]) -> list[str]:
    """All reason/commit step ids in the frozen blueprint (including loop bodies)."""
    ids: list[str] = []
    for raw in frozen_steps:
        if not isinstance(raw, dict):
            continue
        if raw.get("kind") == "loop":
            for body in raw.get("steps") or []:
                if isinstance(body, dict) and body.get("id"):
                    ids.append(str(body["id"]))
        elif raw.get("id"):
            ids.append(str(raw["id"]))
    return ids


async def _resolve_idempotent_start(
    *,
    namespace: str,
    definition_id: str,
    idempotency_key: str | None,
    conn: BaseDBAsyncClient | None = None,
) -> str | None:
    """Return an existing trace_id for this definition+key, or raise on cross-definition reuse."""
    if idempotency_key is None:
        return None

    q_same = SagaInstance.filter(
        namespace=namespace,
        definition_id=definition_id,
        start_idempotency_key=idempotency_key,
    )
    if conn is not None:
        q_same = q_same.using_db(conn)
    existing_same = await q_same.first()
    if existing_same is not None:
        logger.info(
            "Idempotent start: returning existing saga %s for key %s (definition %s)",
            existing_same.trace_id,
            idempotency_key,
            definition_id,
        )
        return existing_same.trace_id

    q_other = SagaInstance.filter(namespace=namespace, start_idempotency_key=idempotency_key)
    if conn is not None:
        q_other = q_other.using_db(conn)
    existing_other = await q_other.first()
    if existing_other is not None and existing_other.definition_id != definition_id:
        raise StartIdempotencyConflictError(
            namespace=namespace,
            idempotency_key=idempotency_key,
            existing_definition_id=existing_other.definition_id,
        )

    return None


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
        raise DefinitionNotFoundError(namespace=namespace, name=name, version=version)
    if not definition.is_active:
        raise InactiveSagaDefinitionError(namespace=namespace, name=name, version=version)
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
    schemas_root: str | None,
    compensations_root: str | None,
    parent_trace_id: str | None = None,
) -> str:
    frozen_steps = list(definition.body.get("steps") or [])
    segment, loop_state = take_initial_materialization_segment(frozen_steps)
    loop_definitions = build_loop_definitions(frozen_steps)
    resolved_assets = await resolve_executable_step_assets(
        segment,
        schemas_root=schemas_root,
        compensations_root=compensations_root,
    )

    trace_id = uuid.uuid4().hex
    step_shells = {
        step_id: {"output": {"data": {}}, "facts": {}}
        for step_id in _executable_shell_ids(frozen_steps)
    }
    context: dict[str, Any] = {
        "input": input or {},
        "steps": step_shells,
        "loops": initial_loops_context(frozen_steps),
    }
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace=namespace,
        definition_id=str(definition.id),
        status=SagaStatus.PENDING,
        context=context,
        frozen_steps=frozen_steps,
        loop_definitions=loop_definitions,
        loop_state=loop_state,
        start_idempotency_key=idempotency_key,
        parent_trace_id=parent_trace_id,
        using_db=conn,
    )
    await get_registry().engine.on_saga_created(
        saga=saga,
        conn=conn,
        definition_id=str(definition.id),
        definition_name=definition.name,
        definition_version=definition.version,
        step_count=len(segment),
    )
    await materialize_executable_steps(
        saga=saga,
        step_specs=segment,
        start_forward_seq=0,
        start_order_index=0,
        conn=conn,
        resolved_assets=resolved_assets,
    )

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
) -> StartSagaResult:
    """Create saga and step instances from definition, emit SAGA_STARTED in one transaction.

    When idempotency_key is provided, if a saga was already started with that key for
    the same definition (namespace, name, version), returns its trace_id without
    creating a new saga. Reusing a key for a different definition raises
    StartIdempotencyConflictError.

    Delay-tail materialization: only the prefix through the first loop's iteration-1
    body (or the full linear blueprint when there is no loop) is created at start.
    """
    definition = await _require_saga_definition(namespace=namespace, name=name, version=version)
    definition_id = str(definition.id)

    existing_trace_id = await _resolve_idempotent_start(
        namespace=namespace,
        definition_id=definition_id,
        idempotency_key=idempotency_key,
    )
    if existing_trace_id is not None:
        return StartSagaResult(existing_trace_id, created=False)

    settings = get_settings()

    async with in_transaction() as conn:
        definition = await _require_saga_definition(
            namespace=namespace,
            name=name,
            version=version,
            conn=conn,
        )
        definition_id = str(definition.id)

        existing_trace_id = await _resolve_idempotent_start(
            namespace=namespace,
            definition_id=definition_id,
            idempotency_key=idempotency_key,
            conn=conn,
        )
        if existing_trace_id is not None:
            return StartSagaResult(existing_trace_id, created=False)

        trace_id = await _create_saga_and_steps(
            conn=conn,
            definition=definition,
            namespace=namespace,
            name=name,
            version=version,
            input=input,
            idempotency_key=idempotency_key,
            schemas_root=settings.schemas_root,
            compensations_root=settings.compensations_root,
        )

    logger.info("Started saga %s (namespace=%s, definition=%s)", trace_id, namespace, name)
    return StartSagaResult(trace_id, created=True)
