"""Tests for GET /v1/health (no DB)."""

import pytest
from engine.api.routes.health import router as health_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def health_app() -> FastAPI:
    app = FastAPI()
    app.include_router(health_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_get_health_200(health_app: FastAPI) -> None:
    transport = ASGITransport(app=health_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
