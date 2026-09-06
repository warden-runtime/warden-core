"""Worker definition lookup must use the full (namespace, name, version) identity."""

from uuid import uuid4

import pytest
from common.catalog_errors import CatalogDefinitionNotFoundError, InactiveCatalogDefinitionError
from common.models import OutboxEvent, ProcessedCommand, ProviderSecret, WorkerDefinition
from common.topics import TOPIC_ORCHESTRATOR_EVENTS
from tests.factories import worker_definition_body
from tests.worker_hydration_helpers import seed_forward_step
from workers.logic import handle_worker_command, load_worker_config


@pytest.mark.asyncio
async def test_load_worker_config_selects_exact_version() -> None:
    """Same worker name with two versions returns the blueprint matching worker_version."""
    await WorkerDefinition.create(
        namespace="default",
        name="versioned-worker",
        version="1.0.0",
        body=worker_definition_body(
            name="versioned-worker",
            version="1.0.0",
            model_name="gpt-4o-mini",
            system_prompt="v1",
        ),
    )
    await WorkerDefinition.create(
        namespace="default",
        name="versioned-worker",
        version="2.0.0",
        body=worker_definition_body(
            name="versioned-worker",
            version="2.0.0",
            model_name="gpt-4o",
            system_prompt="v2",
        ),
    )
    await ProviderSecret.create(
        id=uuid4(),
        namespace="default",
        provider="openai",
        api_key="sk-test",
    )

    worker, _secret = await load_worker_config("versioned-worker", "default", "2.0.0")

    assert worker.version == "2.0.0"
    assert worker.model_name == "gpt-4o"
    assert worker.system_prompt == "v2"


@pytest.mark.asyncio
async def test_load_worker_config_raises_when_version_missing() -> None:
    await WorkerDefinition.create(
        namespace="default",
        name="only-v1",
        version="1.0.0",
        body=worker_definition_body(name="only-v1", version="1.0.0"),
    )
    with pytest.raises(CatalogDefinitionNotFoundError, match="not found"):
        await load_worker_config("only-v1", "default", "9.9.9")


@pytest.mark.asyncio
async def test_handle_worker_command_missing_worker_emits_step_failed_and_releases_claim() -> None:
    """Catalog not-found after claim must not leave the step stuck IN_PROGRESS."""
    trace_id = "a" * 32
    span_id = "b" * 16
    idem = f"{trace_id}-missing-worker"
    await seed_forward_step(
        saga_trace_id=trace_id,
        step_span_id=span_id,
        worker="ghost-worker",
        worker_version="1.0.0",
        prompt_ref="p.j2",
    )

    await handle_worker_command(
        {
            "type": "DO_STEP",
            "namespace": "default",
            "saga_trace_id": trace_id,
            "step_span_id": span_id,
            "worker_name": "ghost-worker",
            "worker_version": "1.0.0",
            "idempotency_key": idem,
            "prompt_ref": "p.j2",
            "arguments": {},
            "tool_specs": [],
        }
    )

    claim = await ProcessedCommand.get_or_none(idempotency_key=idem)
    assert claim is None

    failed = await OutboxEvent.filter(
        saga_trace_id=trace_id,
        destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type="STEP_FAILED",
    ).first()
    assert failed is not None
    payload = failed.payload if isinstance(failed.payload, dict) else {}
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    assert output.get("code") == "CATALOG_DEFINITION_NOT_FOUND"


@pytest.mark.asyncio
async def test_handle_worker_command_inactive_worker_emits_step_failed_and_releases_claim() -> None:
    trace_id = "c" * 32
    span_id = "d" * 16
    idem = f"{trace_id}-inactive-worker"
    await seed_forward_step(
        saga_trace_id=trace_id,
        step_span_id=span_id,
        worker="sleepy-worker",
        worker_version="1.0.0",
        prompt_ref="p.j2",
    )
    await WorkerDefinition.create(
        namespace="default",
        name="sleepy-worker",
        version="1.0.0",
        is_active=False,
        body=worker_definition_body(name="sleepy-worker", version="1.0.0"),
    )

    await handle_worker_command(
        {
            "type": "DO_STEP",
            "namespace": "default",
            "saga_trace_id": trace_id,
            "step_span_id": span_id,
            "worker_name": "sleepy-worker",
            "worker_version": "1.0.0",
            "idempotency_key": idem,
            "prompt_ref": "p.j2",
            "arguments": {},
            "tool_specs": [],
        }
    )

    assert await ProcessedCommand.get_or_none(idempotency_key=idem) is None
    failed = await OutboxEvent.filter(
        saga_trace_id=trace_id,
        destination_topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type="STEP_FAILED",
    ).first()
    assert failed is not None
    payload = failed.payload if isinstance(failed.payload, dict) else {}
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    assert output.get("code") == "INACTIVE_CATALOG_DEFINITION"
    with pytest.raises(InactiveCatalogDefinitionError):
        await load_worker_config("sleepy-worker", "default", "1.0.0")
