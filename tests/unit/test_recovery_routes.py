"""Unit tests for operator recovery HTTP routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from engine.api.routes.recovery import router as recovery_router
from engine.recovery_errors import RecoveryConflictError, RecoveryNotFoundError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def recovery_app() -> FastAPI:
    @asynccontextmanager
    async def noop_lifespan(_app: FastAPI):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(recovery_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_retry_step_route_returns_202(mocker, recovery_app: FastAPI):
    mocker.patch(
        "engine.api.routes.recovery.enqueue_step_retry",
        new_callable=AsyncMock,
        return_value={"status": "requeued"},
    )
    transport = ASGITransport(app=recovery_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/sagas/{'a' * 32}/steps/{'b' * 16}/retry-step",
            json={"force": True, "allow_destructive": True, "reason": "stuck"},
        )
    assert resp.status_code == 202
    assert resp.json() == {"status": "requeued"}


@pytest.mark.asyncio
async def test_retry_step_route_maps_not_found_to_404(mocker, recovery_app: FastAPI):
    mocker.patch(
        "engine.api.routes.recovery.enqueue_step_retry",
        new_callable=AsyncMock,
        side_effect=RecoveryNotFoundError("step missing"),
    )
    transport = ASGITransport(app=recovery_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/sagas/{'a' * 32}/steps/{'b' * 16}/retry-step")
    assert resp.status_code == 404
    assert "step missing" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_retry_step_route_maps_conflict_to_409(mocker, recovery_app: FastAPI):
    mocker.patch(
        "engine.api.routes.recovery.enqueue_step_retry",
        new_callable=AsyncMock,
        side_effect=RecoveryConflictError("claim active"),
    )
    transport = ASGITransport(app=recovery_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/sagas/{'a' * 32}/steps/{'b' * 16}/retry-step")
    assert resp.status_code == 409
    assert "claim active" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_retry_compensation_route_returns_202(mocker, recovery_app: FastAPI):
    mocker.patch(
        "engine.api.routes.recovery.enqueue_compensation_retry",
        new_callable=AsyncMock,
        return_value={"status": "requeued"},
    )
    transport = ASGITransport(app=recovery_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/sagas/{'a' * 32}/steps/{'b' * 16}/retry-compensation",
            json={"force": False, "reason": "stuck comp"},
        )
    assert resp.status_code == 202
    assert resp.json() == {"status": "requeued"}


@pytest.mark.asyncio
async def test_retry_compensation_route_maps_not_found_to_404(mocker, recovery_app: FastAPI):
    mocker.patch(
        "engine.api.routes.recovery.enqueue_compensation_retry",
        new_callable=AsyncMock,
        side_effect=RecoveryNotFoundError("compensation missing"),
    )
    transport = ASGITransport(app=recovery_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/sagas/{'a' * 32}/steps/{'b' * 16}/retry-compensation")
    assert resp.status_code == 404
