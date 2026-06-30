"""Unit tests for engine.api.routes.sagas (POST /v1/sagas/start when mounted like production)."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from engine.api.routes.sagas import router as sagas_router
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
        return_value="a" * 32,
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


@pytest.mark.asyncio
async def test_post_sagas_start_404_when_definition_not_found(mocker, app_no_db):
    """POST /v1/sagas/start returns 404 when start_saga raises ValueError with 'not found'."""
    mocker.patch(
        "engine.api.routes.sagas.start_saga",
        new_callable=AsyncMock,
        side_effect=ValueError("SagaDefinition not found: namespace='default' ..."),
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
    assert "not found" in resp.json().get("detail", "").lower()


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
