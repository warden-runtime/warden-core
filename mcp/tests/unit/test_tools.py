"""Tests for MCP tool handlers."""

from __future__ import annotations

import pytest

from warden_mcp.config import Settings
from warden_mcp.server import mcp


@pytest.fixture(autouse=True)
def _engine_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_URL", "http://engine.test:8000")
    from warden_mcp.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    from warden_mcp.config import get_settings

    return get_settings()


@pytest.mark.asyncio
async def test_warden_ping_ok(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/health",
        json={"status": "ok"},
    )
    result = await mcp.call_tool("warden_ping", {})
    text = result.content[0].text
    assert '"ok": true' in text.lower() or '"ok":true' in text.lower()
    assert "ok" in text


@pytest.mark.asyncio
async def test_warden_ping_transport_error(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_exception(
        __import__("httpx").ConnectError("connection refused"),
        url=f"{settings.engine_url}/v1/health",
    )
    result = await mcp.call_tool("warden_ping", {})
    text = result.content[0].text
    assert '"error": true' in text.lower() or '"error":true' in text.lower()
    assert "transport" in text.lower()


@pytest.mark.asyncio
async def test_warden_start_saga(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas/start",
        method="POST",
        status_code=202,
        json={"trace_id": "a" * 32, "created": True},
    )
    result = await mcp.call_tool(
        "warden_start_saga",
        {
            "name": "minimal-saga",
            "version": "0.0.1",
            "namespace": "default",
            "saga_input": {},
        },
    )
    text = result.content[0].text
    assert "a" * 32 in text
    assert "created" in text.lower()


@pytest.fixture(autouse=True)
async def _reset_shared_http_client() -> None:
    from warden_mcp.client import close_shared_http_client

    await close_shared_http_client()
    yield
    await close_shared_http_client()


@pytest.mark.asyncio
async def test_warden_get_saga_status_found_shape(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas/{'a' * 32}",
        json={"trace_id": "a" * 32, "status": "RUNNING"},
    )
    result = await mcp.call_tool(
        "warden_get_saga_status",
        {"trace_id": "a" * 32},
    )
    text = result.content[0].text
    assert '"found": true' in text.lower() or '"found":true' in text.lower()
    assert '"saga"' in text
    assert '"items"' not in text


@pytest.mark.asyncio
async def test_warden_wait_for_saga_not_found_fail_fast(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas/{'a' * 32}",
        status_code=404,
        json={"detail": "Saga instance not found."},
    )
    result = await mcp.call_tool(
        "warden_wait_for_saga",
        {"trace_id": "a" * 32, "timeout_s": 5.0},
    )
    text = result.content[0].text
    assert "found" in text.lower()
    assert httpx_mock.get_requests().__len__() == 1


@pytest.mark.asyncio
async def test_warden_list_sagas_in_flight(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas",
        match_params={"in_flight": "true", "include_total": "true"},
        json={
            "items": [{"trace_id": "a" * 32, "status": "RUNNING"}],
            "limit": 50,
            "offset": 0,
            "has_more": False,
            "total": 1,
        },
    )
    result = await mcp.call_tool(
        "warden_list_sagas",
        {"in_flight": True, "include_total": True},
    )
    text = result.content[0].text
    assert "RUNNING" in text
    assert "total" in text


@pytest.mark.asyncio
async def test_warden_wait_for_saga_done_includes_slim_steps(
    httpx_mock,
    settings: Settings,
) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas/{'a' * 32}",
        json={"trace_id": "a" * 32, "status": "COMPLETED"},
    )
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas/{'a' * 32}/steps",
        json={
            "items": [
                {
                    "step_id": "greet",
                    "step_span_id": "b" * 16,
                    "status": "COMPLETED",
                    "step_kind": "reason",
                    "order_index": 0,
                    "timing": {"worker": {"tool_ms": 1}},
                    "error_details": None,
                }
            ],
            "limit": 50,
            "offset": 0,
            "has_more": False,
        },
    )
    result = await mcp.call_tool(
        "warden_wait_for_saga",
        {"trace_id": "a" * 32, "timeout_s": 5.0},
    )
    text = result.content[0].text
    assert '"done": true' in text.lower() or '"done":true' in text.lower()
    assert "greet" in text
    assert "COMPLETED" in text
    assert "tool_ms" not in text


@pytest.mark.asyncio
async def test_warden_wait_for_saga_timeout_includes_steps(
    httpx_mock,
    settings: Settings,
) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas/{'a' * 32}",
        is_reusable=True,
        json={"trace_id": "a" * 32, "status": "RUNNING"},
    )
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/sagas/{'a' * 32}/steps",
        json={
            "items": [
                {
                    "step_id": "greet",
                    "step_span_id": "c" * 16,
                    "status": "IN_PROGRESS",
                    "step_kind": "reason",
                    "order_index": 0,
                }
            ],
            "limit": 50,
            "offset": 0,
            "has_more": False,
        },
    )
    result = await mcp.call_tool(
        "warden_wait_for_saga",
        {"trace_id": "a" * 32, "timeout_s": 0.05, "poll_interval_s": 0.01},
    )
    text = result.content[0].text.lower()
    assert "timed_out" in text
    assert "greet" in text
    assert "in_progress" in text


@pytest.mark.asyncio
async def test_warden_deploy_manifest_dry_run(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/manifests?dry_run=true",
        method="POST",
        json={
            "message": "Worker 'x' dry-run OK (not registered)",
            "dry_run": True,
        },
    )
    result = await mcp.call_tool(
        "warden_deploy_manifest",
        {
            "yaml_content": "kind: worker\nname: x\nprovider: mock\nmodel_name: demo\n",
            "dry_run": True,
        },
    )
    text = result.content[0].text.lower()
    assert "dry-run ok" in text or '"dry_run": true' in text or '"dry_run":true' in text
    req = httpx_mock.get_requests()[0]
    assert "dry_run=true" in str(req.url)


@pytest.mark.asyncio
async def test_warden_deploy_manifest_requires_non_empty_content() -> None:
    result = await mcp.call_tool("warden_deploy_manifest", {"yaml_content": "   "})
    text = result.content[0].text
    assert "non-empty" in text.lower()


@pytest.mark.asyncio
async def test_warden_deploy_manifest_rejects_oversized_content(
    httpx_mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WARDEN_MCP_MANIFEST_MAX_BYTES", "32")
    from warden_mcp.config import get_settings

    get_settings.cache_clear()
    result = await mcp.call_tool(
        "warden_deploy_manifest",
        {"yaml_content": "kind: worker\nname: x\n" + ("x" * 64)},
    )
    text = result.content[0].text
    assert "exceeds limit" in text.lower()
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_warden_get_saga_status_rejects_invalid_trace_id(
    httpx_mock,
    settings: Settings,
) -> None:
    result = await mcp.call_tool(
        "warden_get_saga_status",
        {"trace_id": "not-valid"},
    )
    text = result.content[0].text
    assert "trace_id" in text.lower()
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_list_tools_includes_core_operations() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "warden_ping" in names
    assert "warden_ready" in names
    assert "warden_start_saga" in names
    assert "warden_list_sagas" in names
    assert "warden_get_worker_definition" in names
    assert "warden_list_step_definitions" in names
    assert "warden_get_step_definition" in names
    assert "warden_set_definition_active" in names
    assert "warden_list_pending_reviews" in names
    assert "warden_retry_stuck_step" in names
    assert "warden_wait_for_review" in names
    assert "warden_start_and_wait" in names


@pytest.mark.asyncio
async def test_warden_ready_ok(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/health/ready",
        json={"status": "ready", "database": "ok"},
    )
    result = await mcp.call_tool("warden_ready", {})
    text = result.content[0].text
    assert '"ok": true' in text.lower() or '"ok":true' in text.lower()
    assert "ready" in text.lower()


@pytest.mark.asyncio
async def test_warden_ready_503(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/health/ready",
        status_code=503,
        json={"detail": "Database not ready: connection refused"},
    )
    result = await mcp.call_tool("warden_ready", {})
    text = result.content[0].text
    assert '"error": true' in text.lower() or '"error":true' in text.lower()
    assert "503" in text


@pytest.mark.asyncio
async def test_warden_list_step_definitions(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/definitions/steps",
        match_params={"is_active": "true", "include_total": "true"},
        json={
            "items": [{"id": "00000000-0000-4000-8000-000000000001", "name": "greet"}],
            "limit": 50,
            "offset": 0,
            "has_more": False,
            "total": 1,
        },
    )
    result = await mcp.call_tool(
        "warden_list_step_definitions",
        {"is_active": True, "include_total": True},
    )
    text = result.content[0].text
    assert "greet" in text
    assert "total" in text


@pytest.mark.asyncio
async def test_warden_get_step_definition(httpx_mock, settings: Settings) -> None:
    def_id = "00000000-0000-4000-8000-000000000001"
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/definitions/steps",
        match_params={"id": def_id, "include_body": "true"},
        json={"id": def_id, "name": "greet", "body": {"kind": "step"}},
    )
    result = await mcp.call_tool(
        "warden_get_step_definition",
        {"definition_id": def_id, "include_body": True},
    )
    text = result.content[0].text
    assert "greet" in text
    assert "kind" in text


@pytest.mark.asyncio
async def test_warden_get_saga_definition_by_triple(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/definitions/sagas",
        match_params={
            "namespace": "default",
            "name": "demo",
            "version": "1.0.0",
        },
        json={"id": "00000000-0000-4000-8000-000000000002", "name": "demo", "version": "1.0.0"},
    )
    result = await mcp.call_tool(
        "warden_get_saga_definition",
        {"namespace": "default", "name": "demo", "version": "1.0.0"},
    )
    text = result.content[0].text
    assert "demo" in text
    assert "items" not in text


@pytest.mark.asyncio
async def test_warden_get_definition_partial_triple_validation() -> None:
    result = await mcp.call_tool(
        "warden_get_saga_definition",
        {"namespace": "default", "name": "demo"},
    )
    text = result.content[0].text.lower()
    assert "version" in text or "triple" in text
    assert "error" in text


@pytest.mark.asyncio
async def test_warden_set_definition_active_by_id(httpx_mock, settings: Settings) -> None:
    def_id = "00000000-0000-4000-8000-000000000001"
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/definitions/workers",
        method="PATCH",
        match_params={"id": def_id},
        json={"id": def_id, "is_active": False, "name": "mock-worker"},
    )
    result = await mcp.call_tool(
        "warden_set_definition_active",
        {"kind": "workers", "is_active": False, "definition_id": def_id},
    )
    text = result.content[0].text
    assert "mock-worker" in text
    request = httpx_mock.get_requests()[0]
    assert request.method == "PATCH"
    assert "id=" in str(request.url)


@pytest.mark.asyncio
async def test_warden_set_definition_active_requires_identity() -> None:
    result = await mcp.call_tool(
        "warden_set_definition_active",
        {"kind": "steps", "is_active": True},
    )
    text = result.content[0].text
    assert "definition_id" in text.lower() or "namespace" in text.lower()


@pytest.mark.asyncio
async def test_warden_deploy_surfaces_catalog_error(httpx_mock, settings: Settings) -> None:
    httpx_mock.add_response(
        url=f"{settings.engine_url}/v1/manifests",
        method="POST",
        status_code=404,
        json={
            "detail": {
                "code": "CATALOG_DEFINITION_NOT_FOUND",
                "message": "Step definition not found: namespace='default', name='missing', version='0.1.0'",
                "kind": "step",
                "namespace": "default",
                "name": "missing",
                "version": "0.1.0",
            }
        },
    )
    result = await mcp.call_tool(
        "warden_deploy_manifest",
        {"yaml_content": "kind: saga\nname: x\nversion: 0.1.0\nsteps: []\n"},
    )
    text = result.content[0].text.lower()
    assert "catalog_definition_not_found" in text or "catalog_definition_not_found" in text.replace(
        '"', ""
    )
    assert "catalog_kind" in text or '"step"' in text
