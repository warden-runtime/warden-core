"""Tests for GET /v1/health and /v1/health/ready."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from engine.api.routes.health import router as health_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def health_app() -> FastAPI:
    @asynccontextmanager
    async def noop_lifespan(_app: FastAPI):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(health_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_get_health_ok(health_app: FastAPI) -> None:
    transport = ASGITransport(app=health_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_get_health_ready_ok(mocker, health_app: FastAPI) -> None:
    conn = MagicMock()
    conn.execute_query = AsyncMock(return_value=None)
    mocker.patch(
        "engine.api.routes.health.Tortoise.get_connection",
        return_value=conn,
    )
    transport = ASGITransport(app=health_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["database"] == "ok"


@pytest.mark.asyncio
async def test_get_health_ready_503_on_db_error(mocker, health_app: FastAPI) -> None:
    conn = MagicMock()
    conn.execute_query = AsyncMock(side_effect=RuntimeError("connection refused"))
    mocker.patch(
        "engine.api.routes.health.Tortoise.get_connection",
        return_value=conn,
    )
    transport = ASGITransport(app=health_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/health/ready")
    assert resp.status_code == 503
    assert "database" in resp.json()["detail"].lower()
