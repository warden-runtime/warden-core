"""Unit tests for engine.api.routes.manifests (POST /v1/manifests)."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from engine.api.routes.manifests import router as manifests_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_manifests_no_db(mocker):
    """FastAPI app with /v1 prefix and mocked RegistryService."""

    @asynccontextmanager
    async def noop_lifespan(a: FastAPI):
        yield

    app = FastAPI(title="Test", lifespan=noop_lifespan)
    app.include_router(manifests_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_post_v1_manifests_200_yaml(mocker, app_manifests_no_db):
    """POST /v1/manifests with YAML body returns 200 and message."""
    mock_registry = mocker.MagicMock()
    mock_registry.register_manifest_from_dict = AsyncMock(
        return_value="Worker 'email-worker' registered successfully"
    )
    mocker.patch(
        "engine.api.routes.manifests.RegistryService",
        return_value=mock_registry,
    )
    yaml_body = "kind: worker\nname: email-worker\nprovider: openai\nmodel_name: gpt-4o\n"
    with TestClient(app_manifests_no_db) as c:
        resp = c.post(
            "/v1/manifests",
            content=yaml_body,
            headers={"Content-Type": "application/x-yaml"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "Worker 'email-worker' registered successfully"
    mock_registry.register_manifest_from_dict.assert_called_once()
    call_args = mock_registry.register_manifest_from_dict.call_args[0][0]
    assert call_args["kind"] == "worker"
    assert call_args["name"] == "email-worker"


@pytest.mark.asyncio
async def test_post_v1_manifests_200_json(mocker, app_manifests_no_db):
    """POST /v1/manifests with JSON body returns 200 and message."""
    mock_registry = mocker.MagicMock()
    mock_registry.register_manifest_from_dict = AsyncMock(
        return_value="Saga 'my-saga' v1.0.0 registered successfully"
    )
    mocker.patch(
        "engine.api.routes.manifests.RegistryService",
        return_value=mock_registry,
    )
    with TestClient(app_manifests_no_db) as c:
        resp = c.post(
            "/v1/manifests",
            json={"kind": "saga", "name": "my-saga", "version": "1.0.0", "steps": []},
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert "my-saga" in resp.json()["message"]
    call_args = mock_registry.register_manifest_from_dict.call_args[0][0]
    assert call_args["kind"] == "saga"


@pytest.mark.asyncio
async def test_post_v1_manifests_400_on_validation_error(mocker, app_manifests_no_db):
    """POST /v1/manifests returns 400 when registry raises ValueError."""
    mock_registry = mocker.MagicMock()
    mock_registry.register_manifest_from_dict = AsyncMock(
        side_effect=ValueError("Unknown manifest kind: 'invalid'.")
    )
    mocker.patch(
        "engine.api.routes.manifests.RegistryService",
        return_value=mock_registry,
    )
    with TestClient(app_manifests_no_db) as c:
        resp = c.post(
            "/v1/manifests",
            json={"kind": "invalid", "name": "x"},
        )
    assert resp.status_code == 400
    assert "detail" in resp.json()
    assert "unknown" in resp.json()["detail"].lower()
