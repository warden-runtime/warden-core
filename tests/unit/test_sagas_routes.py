"""Unit tests for engine.api.routes.sagas (POST /v1/sagas/start when mounted like production)."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from engine.api.routes.sagas import router as sagas_router
from engine.api.saga_errors import DefinitionNotFoundError
from engine.api.saga_start import StartSagaResult
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_no_db():
    """FastAPI app with no-op lifespan so TestClient does not require DB."""

    @asynccontextmanager
    async def noop_lifespan(a: FastAPI):
        yield

    app = FastAPI(title="Test", lifespan=noop_lifespan)
    app.include_router(sagas_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_post_sagas_start_202_returns_trace_id(mocker, app_no_db):
    """POST /v1/sagas/start returns 202 with trace_id when start_saga succeeds."""
    mocker.patch(
        "engine.api.routes.sagas.start_saga",
        new_callable=AsyncMock,
        return_value=StartSagaResult(trace_id="a" * 32, created=True),
    )
    with TestClient(app_no_db) as c:
        resp = c.post(
            "/v1/sagas/start",
            json={
                "namespace": "default",
                "name": "test-saga",
                "version": "1.0.0",
                "input": {"key": "value"},
            },
        )
    assert resp.status_code == 202
    data = resp.json()
    assert "trace_id" in data
    assert data["trace_id"] == "a" * 32
    assert data["created"] is True


@pytest.mark.asyncio
async def test_post_sagas_start_404_when_definition_not_found(mocker, app_no_db):
    """POST /v1/sagas/start returns 404 when start_saga raises DefinitionNotFoundError."""
    mocker.patch(
        "engine.api.routes.sagas.start_saga",
        new_callable=AsyncMock,
        side_effect=DefinitionNotFoundError(
            namespace="default",
            name="missing",
            version="1.0.0",
        ),
    )
    with TestClient(app_no_db) as c:
        resp = c.post(
            "/v1/sagas/start",
            json={
                "namespace": "default",
                "name": "missing",
                "version": "1.0.0",
                "input": {},
            },
        )
    assert resp.status_code == 404
    detail = resp.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["code"] == "CATALOG_DEFINITION_NOT_FOUND"
    assert "not found" in detail["message"].lower()


@pytest.mark.asyncio
async def test_post_sagas_start_409_on_idempotency_conflict(mocker, app_no_db):
    """POST /v1/sagas/start returns 409 when start_saga raises StartIdempotencyConflictError."""
    from engine.api.saga_errors import StartIdempotencyConflictError

    mocker.patch(
        "engine.api.routes.sagas.start_saga",
        new_callable=AsyncMock,
        side_effect=StartIdempotencyConflictError(
            namespace="default",
            idempotency_key="dup",
            existing_definition_id="00000000-0000-4000-8000-000000000001",
        ),
    )
    with TestClient(app_no_db) as c:
        resp = c.post(
            "/v1/sagas/start",
            json={
                "namespace": "default",
                "name": "other",
                "version": "1.0.0",
                "input": {},
                "idempotency_key": "dup",
            },
        )
    assert resp.status_code == 409
    assert "idempotency key" in resp.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_post_sagas_start_409_on_inactive_definition(mocker, app_no_db):
    """POST /v1/sagas/start returns 409 when start_saga raises InactiveSagaDefinitionError."""
    from engine.api.saga_errors import InactiveSagaDefinitionError

    mocker.patch(
        "engine.api.routes.sagas.start_saga",
        new_callable=AsyncMock,
        side_effect=InactiveSagaDefinitionError(
            namespace="default",
            name="inactive",
            version="1.0.0",
        ),
    )
    with TestClient(app_no_db) as c:
        resp = c.post(
            "/v1/sagas/start",
            json={
                "namespace": "default",
                "name": "inactive",
                "version": "1.0.0",
                "input": {},
            },
        )
    assert resp.status_code == 409
    detail = resp.json().get("detail")
    assert isinstance(detail, dict)
    assert detail["code"] == "INACTIVE_CATALOG_DEFINITION"
    assert detail["kind"] == "saga"
    assert "inactive" in detail["message"].lower()


@pytest.mark.asyncio
async def test_post_sagas_start_400_on_other_value_error(mocker, app_no_db):
    """POST /v1/sagas/start returns 400 when start_saga raises other ValueError."""
    mocker.patch(
        "engine.api.routes.sagas.start_saga",
        new_callable=AsyncMock,
        side_effect=ValueError("Invalid input"),
    )
    with TestClient(app_no_db) as c:
        resp = c.post(
            "/v1/sagas/start",
            json={
                "namespace": "default",
                "name": "test",
                "version": "1.0.0",
                "input": {},
            },
        )
    assert resp.status_code == 400
    assert "invalid" in resp.json().get("detail", "").lower()
