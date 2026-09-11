"""Tests for the engine HTTP client."""

from __future__ import annotations

import json

import pytest

from warden_mcp.client import EngineClient
from warden_mcp.config import Settings, get_settings
from warden_mcp.errors import (
    EngineAPIError,
    EngineTransportError,
    engine_error_payload,
    format_api_detail,
    parse_engine_error_body,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        engine_url="http://engine.test:8000",
        http_timeout_s=5.0,
    )


@pytest.fixture
def client(settings: Settings) -> EngineClient:
    return EngineClient(settings=settings)


def test_engine_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGINE_URL", raising=False)
    monkeypatch.delenv("WARDEN_MCP_ENGINE_URL", raising=False)
    get_settings.cache_clear()
    settings = Settings(_env_file=None)
    assert settings.engine_url == "http://127.0.0.1:8000"


def test_format_api_detail_string_detail() -> None:
    body = '{"detail": "Saga definition not found"}'
    assert format_api_detail(body, status_code=404) == "Saga definition not found"


def test_format_api_detail_non_json() -> None:
    assert format_api_detail("bad gateway", status_code=502) == "bad gateway"


def test_parse_catalog_error_detail() -> None:
    body = json.dumps(
        {
            "detail": {
                "code": "CATALOG_DEFINITION_NOT_FOUND",
                "message": "Step definition not found: namespace='default', name='x', version='1.0.0'",
                "kind": "step",
                "namespace": "default",
                "name": "x",
                "version": "1.0.0",
            }
        }
    )
    message, fields = parse_engine_error_body(body, status_code=404)
    assert "not found" in message.lower()
    assert fields["code"] == "CATALOG_DEFINITION_NOT_FOUND"
    assert fields["catalog_kind"] == "step"
    assert fields["name"] == "x"


def test_engine_error_payload_includes_catalog_fields() -> None:
    exc = EngineAPIError(
        status_code=409,
        method="POST",
        path="/v1/manifests",
        detail="Worker definition is inactive",
        fields={
            "code": "INACTIVE_CATALOG_DEFINITION",
            "catalog_kind": "worker",
            "namespace": "default",
            "name": "w",
            "version": "1.0.0",
        },
    )
    payload = engine_error_payload(exc)
    assert payload["kind"] == "http"
    assert payload["code"] == "INACTIVE_CATALOG_DEFINITION"
    assert payload["catalog_kind"] == "worker"


@pytest.mark.asyncio
async def test_get_json_success(client: EngineClient, httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://engine.test:8000/v1/health",
        json={"status": "ok"},
    )
    data = await client.get_json("/v1/health")
    assert data == {"status": "ok"}


@pytest.mark.asyncio
async def test_get_json_raises_on_error(client: EngineClient, httpx_mock) -> None:
    httpx_mock.add_response(
        status_code=404,
        json={"detail": "Saga instance not found."},
    )
    with pytest.raises(EngineAPIError) as exc_info:
        await client.get_json("/v1/sagas", params=[("trace_id", "a" * 32)])
    assert exc_info.value.status_code == 404
    assert "Saga instance not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_json_raises_transport_error_on_connect_failure(
    client: EngineClient,
    httpx_mock,
) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    with pytest.raises(EngineTransportError) as exc_info:
        await client.get_json("/v1/health")
    assert "connection refused" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_post_json_manifest(client: EngineClient, httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://engine.test:8000/v1/manifests",
        method="POST",
        json={"message": "registered"},
    )
    data = await client.post_json(
        "/v1/manifests",
        content=b"kind: worker\n",
        headers={"Content-Type": "application/x-yaml"},
    )
    assert data == {"message": "registered"}
    request = httpx_mock.get_requests()[0]
    assert request.headers["content-type"] == "application/x-yaml"


@pytest.mark.asyncio
async def test_patch_json_set_active(client: EngineClient, httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://engine.test:8000/v1/definitions/steps",
        method="PATCH",
        match_params={"id": "00000000-0000-4000-8000-000000000001"},
        json={"id": "00000000-0000-4000-8000-000000000001", "is_active": False},
    )
    data = await client.patch_json(
        "/v1/definitions/steps",
        json_body={"is_active": False},
        params=[("id", "00000000-0000-4000-8000-000000000001")],
    )
    assert data["is_active"] is False
    request = httpx_mock.get_requests()[0]
    assert request.method == "PATCH"
