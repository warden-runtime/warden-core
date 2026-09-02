"""Unit tests for engine.api.saga_start idempotency and definition guards."""

from __future__ import annotations

import pytest
from common.models import SagaDefinition, SagaInstance
from engine.api.saga_errors import InactiveSagaDefinitionError, StartIdempotencyConflictError
from engine.api.saga_start import start_saga

_EMPTY_BODY = {"steps": []}


@pytest.mark.asyncio
async def test_start_saga_idempotent_same_definition_returns_same_trace_id() -> None:
    await SagaDefinition.create(
        namespace="default",
        name="idem-saga",
        version="1.0.0",
        is_active=True,
        body=_EMPTY_BODY,
    )
    first = await start_saga(
        namespace="default",
        name="idem-saga",
        version="1.0.0",
        input={},
        idempotency_key="client-key-1",
    )
    second = await start_saga(
        namespace="default",
        name="idem-saga",
        version="1.0.0",
        input={"other": True},
        idempotency_key="client-key-1",
    )
    assert first.trace_id == second.trace_id
    assert first.created is True
    assert second.created is False
    assert await SagaInstance.filter(start_idempotency_key="client-key-1").count() == 1


@pytest.mark.asyncio
async def test_start_saga_idempotency_conflict_across_definitions() -> None:
    def_a = await SagaDefinition.create(
        namespace="default",
        name="saga-a",
        version="1.0.0",
        is_active=True,
        body=_EMPTY_BODY,
    )
    await SagaDefinition.create(
        namespace="default",
        name="saga-b",
        version="1.0.0",
        is_active=True,
        body=_EMPTY_BODY,
    )
    await start_saga(
        namespace="default",
        name="saga-a",
        version="1.0.0",
        input={},
        idempotency_key="shared-key",
    )
    with pytest.raises(StartIdempotencyConflictError) as exc_info:
        await start_saga(
            namespace="default",
            name="saga-b",
            version="1.0.0",
            input={},
            idempotency_key="shared-key",
        )
    assert exc_info.value.existing_definition_id == str(def_a.id)


@pytest.mark.asyncio
async def test_start_saga_different_keys_create_two_instances() -> None:
    await SagaDefinition.create(
        namespace="default",
        name="multi-start",
        version="1.0.0",
        is_active=True,
        body=_EMPTY_BODY,
    )
    first = await start_saga(
        namespace="default",
        name="multi-start",
        version="1.0.0",
        input={},
        idempotency_key="key-a",
    )
    second = await start_saga(
        namespace="default",
        name="multi-start",
        version="1.0.0",
        input={},
        idempotency_key="key-b",
    )
    assert first.trace_id != second.trace_id
    assert await SagaInstance.filter(definition_id__isnull=False).count() == 2


@pytest.mark.asyncio
async def test_start_saga_rejects_inactive_definition() -> None:
    await SagaDefinition.create(
        namespace="default",
        name="inactive-saga",
        version="1.0.0",
        is_active=False,
        body=_EMPTY_BODY,
    )
    with pytest.raises(InactiveSagaDefinitionError):
        await start_saga(
            namespace="default",
            name="inactive-saga",
            version="1.0.0",
            input={},
        )
