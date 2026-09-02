"""422 validation for HITL and recovery mutation path parameters."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from engine.api.routes.human_gate import router as human_gate_router
from engine.api.routes.recovery import router as recovery_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mutation_app() -> FastAPI:
    @asynccontextmanager
    async def noop_lifespan(_app: FastAPI):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(human_gate_router, prefix="/v1")
    app.include_router(recovery_router, prefix="/v1")
    return app


_TRACE = "a" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/sagas/not-hex/steps/1111111111111111/decision"),
        ("POST", f"/v1/sagas/{_TRACE}/steps/not-hex/decision"),
        ("POST", "/v1/sagas/not-hex/steps/1111111111111111/retry"),
        ("POST", f"/v1/sagas/{_TRACE}/steps/not-hex/retry"),
        ("POST", "/v1/sagas/not-hex/steps/bbbbbbbbbbbbbbbb/retry-step"),
        ("POST", f"/v1/sagas/{_TRACE}/steps/not-hex/retry-compensation"),
    ],
)
async def test_mutation_routes_reject_invalid_path_ids(
    mutation_app: FastAPI,
    method: str,
    path: str,
) -> None:
    transport = ASGITransport(app=mutation_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.request(method, path, json={})
    assert resp.status_code == 422
