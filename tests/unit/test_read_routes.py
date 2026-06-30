"""Tests for GET read routes (definitions + saga instance list)."""

from contextlib import asynccontextmanager

import pytest
from common.models import SagaDefinition, SagaInstance, SagaStatus, SagaStepInstance, StepStatus
from engine.api.routes.definitions import router as definitions_router
from engine.api.routes.sagas import router as sagas_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def read_app():
    """FastAPI app with read routers; Tortoise from tests/conftest autouse."""

    @asynccontextmanager
    async def noop_lifespan(_: FastAPI):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(definitions_router, prefix="/v1")
    app.include_router(sagas_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_get_definitions_saga_by_id_404(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/definitions/sagas/00000000-0000-4000-8000-000000000001",
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_definitions_saga_by_id_404_invalid_uuid(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas/not-a-uuid")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_definitions_saga_by_id_200(read_app):
    row = await SagaDefinition.create(
        namespace="default",
        name="by-id",
        version="2.0.0",
        is_active=True,
        body={"steps": []},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/definitions/sagas/{row.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "by-id"
    assert data["version"] == "2.0.0"
    assert data["id"] == str(row.id)


@pytest.mark.asyncio
async def test_get_definitions_sagas_empty(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["limit"] == 50
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_get_definitions_sagas_limit_invalid(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas", params={"limit": 0})
    assert resp.status_code == 422
    assert "limit" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_sagas_in_flight_and_status_conflict(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/sagas",
            params=[("in_flight", "true"), ("status", "FAILED")],
        )
    assert resp.status_code == 400
    assert "in_flight" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_sagas_invalid_status(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas", params=[("status", "NOT_A_STATUS")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "Invalid" in detail or "invalid" in detail


@pytest.mark.asyncio
async def test_get_sagas_in_flight_filters(read_app):
    await SagaInstance.create(
        trace_id="a" * 32,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.COMPLETED,
        context={},
    )
    await SagaInstance.create(
        trace_id="b" * 32,
        namespace="default",
        definition_id="def-2",
        status=SagaStatus.RUNNING,
        context={},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas", params={"in_flight": "true"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    trace_ids = {it["trace_id"] for it in items}
    assert "b" * 32 in trace_ids
    assert "a" * 32 not in trace_ids


@pytest.mark.asyncio
async def test_get_sagas_trace_id_filter(read_app):
    target = "c" * 32
    other = "d" * 32
    await SagaInstance.create(
        trace_id=target,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.COMPLETED,
        context={},
    )
    await SagaInstance.create(
        trace_id=other,
        namespace="default",
        definition_id="def-2",
        status=SagaStatus.RUNNING,
        context={},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas", params={"trace_id": target})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["trace_id"] == target


@pytest.mark.asyncio
async def test_get_sagas_trace_id_invalid(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas", params={"trace_id": "not-hex"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_saga_steps_ordered(read_app):
    trace_id = "e" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.RUNNING,
        context={},
    )
    await SagaStepInstance.create(
        span_id="1111111111111111",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="second",
        step_name="second",
        order_index=1,
        idempotency_key="k2",
        status=StepStatus.PENDING,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    await SagaStepInstance.create(
        span_id="2222222222222222",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="first",
        step_name="first",
        order_index=0,
        idempotency_key="k1",
        status=StepStatus.COMPLETED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas/steps", params={"trace_id": trace_id})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it["step_id"] for it in items] == ["first", "second"]
    assert items[0]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_get_saga_steps_not_found(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas/steps", params={"trace_id": "f" * 32})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_saga_steps_status_filter(read_app):
    trace_id = "0" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.RUNNING,
        context={},
    )
    await SagaStepInstance.create(
        span_id="aaaaaaaaaaaaaaaa",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="done",
        step_name="done",
        order_index=0,
        idempotency_key="done-key",
        status=StepStatus.COMPLETED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    await SagaStepInstance.create(
        span_id="bbbbbbbbbbbbbbbb",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="open",
        step_name="open",
        order_index=1,
        idempotency_key="open-key",
        status=StepStatus.IN_PROGRESS,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/sagas/steps",
            params=[("trace_id", trace_id), ("status", "IN_PROGRESS")],
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["step_id"] == "open"


@pytest.mark.asyncio
async def test_get_saga_steps_includes_timing_on_undo_row(read_app):
    trace_id = "e" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.COMPENSATING,
        context={},
    )
    forward_span = "ffffffffffffffff"
    undo_span = "eeeeeeeeeeeeeeee"
    timing = {
        "worker": {"tool_ms": 42},
        "engine": {"schedule_ms": 11, "dispatch_to_ingest_ms": 300},
    }
    await SagaStepInstance.create(
        span_id=forward_span,
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="forward",
        step_name="forward",
        order_index=0,
        idempotency_key="fwd-key",
        status=StepStatus.FAILED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    await SagaStepInstance.create(
        span_id=undo_span,
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="forward",
        step_name="forward",
        order_index=0,
        idempotency_key="undo-key",
        status=StepStatus.COMPENSATED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
        compensates_span_id=forward_span,
        execution_timing=timing,
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas/steps", params={"trace_id": trace_id})
    assert resp.status_code == 200
    undo = next(it for it in resp.json()["items"] if it["step_span_id"] == undo_span)
    assert undo["compensates_span_id"] == forward_span
    assert undo["timing"] == timing


@pytest.mark.asyncio
async def test_get_saga_steps_includes_error_details(read_app):
    trace_id = "d" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.FAILED,
        context={},
    )
    error_details = {
        "code": "POLICY_REASON_DENIED",
        "message": "policy cel returned false; reason output not allowed",
    }
    await SagaStepInstance.create(
        span_id="cccccccccccccccc",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="denied",
        step_name="denied",
        order_index=0,
        idempotency_key="denied-key",
        status=StepStatus.FAILED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
        error_details=error_details,
    )
    await SagaStepInstance.create(
        span_id="dddddddddddddddd",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="ok",
        step_name="ok",
        order_index=1,
        idempotency_key="ok-key",
        status=StepStatus.COMPLETED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas/steps", params={"trace_id": trace_id})
    assert resp.status_code == 200
    by_step = {it["step_id"]: it for it in resp.json()["items"]}
    assert by_step["denied"]["error_details"] == error_details
    assert by_step["ok"]["error_details"] is None


@pytest.mark.asyncio
async def test_get_saga_step_detail_returns_payloads(read_app):
    trace_id = "a" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.COMPLETED,
        context={},
    )
    resolved = {"name": "Ada"}
    output_payload = {"output": {"data": {"greeting": "Hello, Ada!"}}}
    await SagaStepInstance.create(
        span_id="1111111111111111",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="greet",
        step_name="greet",
        order_index=0,
        idempotency_key="greet-key",
        status=StepStatus.COMPLETED,
        worker="mock-mcp-worker",
        worker_version="0.1.0",
        step_kind="reason",
        timeout_seconds=60,
        resolved_arguments=resolved,
        output_payload=output_payload,
        prompt_ref="mock-greet.j2",
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get("/v1/sagas/steps", params={"trace_id": trace_id})
        detail_resp = await client.get(
            f"/v1/sagas/{trace_id}/steps/1111111111111111",
            params={"namespace": "default"},
        )
    assert list_resp.status_code == 200
    list_item = list_resp.json()["items"][0]
    assert "resolved_arguments" not in list_item
    assert "output_payload" not in list_item
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["resolved_arguments"] == resolved
    assert detail["output_payload"] == output_payload
    assert detail["prompt_ref"] == "mock-greet.j2"


@pytest.mark.asyncio
async def test_get_saga_step_detail_failed_includes_error_details(read_app):
    trace_id = "b" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.FAILED,
        context={},
    )
    error_details = {"code": "TOOL_ERROR", "message": "echo failed", "tool": "echo"}
    await SagaStepInstance.create(
        span_id="2222222222222222",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="greet",
        step_name="greet",
        order_index=0,
        idempotency_key="fail-key",
        status=StepStatus.FAILED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
        error_details=error_details,
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{trace_id}/steps/2222222222222222")
    assert resp.status_code == 200
    assert resp.json()["error_details"] == error_details
    assert resp.json()["status"] == "FAILED"


@pytest.mark.asyncio
async def test_get_saga_step_detail_saga_not_found(read_app):
    transport = ASGITransport(app=read_app)
    trace_id = "c" * 32
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{trace_id}/steps/3333333333333333")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_saga_step_detail_step_not_found(read_app):
    trace_id = "d" * 32
    await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.RUNNING,
        context={},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{trace_id}/steps/4444444444444444")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_saga_step_detail_invalid_ids(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bad_trace = await client.get("/v1/sagas/not-hex/steps/1111111111111111")
        bad_span = await client.get(f"/v1/sagas/{'e' * 32}/steps/not-hex-span")
    assert bad_trace.status_code == 422
    assert bad_span.status_code == 422


@pytest.mark.asyncio
async def test_list_saga_step_instances_by_step_id_orders_recent_first(read_app):
    from engine.api import read_queries

    trace_id = "f" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.COMPENSATING,
        context={},
    )
    await SagaStepInstance.create(
        span_id="aaaaaaaaaaaaaaaa",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="greet",
        step_name="greet",
        order_index=0,
        idempotency_key="older",
        status=StepStatus.FAILED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    await SagaStepInstance.create(
        span_id="bbbbbbbbbbbbbbbb",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="greet",
        step_name="greet",
        order_index=0,
        idempotency_key="newer",
        status=StepStatus.COMPENSATED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
        compensates_span_id="aaaaaaaaaaaaaaaa",
    )
    rows = await read_queries.list_saga_step_instances_by_step_id(
        saga_trace_id=trace_id,
        step_id="greet",
        namespace="default",
    )
    assert len(rows) == 2
    assert rows[0].span_id == "bbbbbbbbbbbbbbbb"


@pytest.mark.asyncio
async def test_get_definitions_sagas_returns_row(read_app):
    await SagaDefinition.create(
        namespace="default",
        name="demo",
        version="1.0.0",
        is_active=True,
        body={"steps": []},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas", params={"namespace": "default"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "demo"
    assert items[0]["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_list_saga_step_instances_by_step_id_tiebreaks_started_at(read_app):
    from engine.api import read_queries

    trace_id = "9" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.RUNNING,
        context={},
    )
    # same started_at via explicit create order; Tortoise auto_now_add may differ by microseconds
    row_a = await SagaStepInstance.create(
        span_id="aaaaaaaaaaaaaaaa",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="greet",
        step_name="greet",
        order_index=0,
        idempotency_key="tie-a",
        status=StepStatus.COMPLETED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    row_b = await SagaStepInstance.create(
        span_id="bbbbbbbbbbbbbbbb",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="greet",
        step_name="greet",
        order_index=0,
        idempotency_key="tie-b",
        status=StepStatus.COMPLETED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    rows = await read_queries.list_saga_step_instances_by_step_id(
        saga_trace_id=trace_id,
        step_id="greet",
        namespace="default",
    )
    assert len(rows) == 2
    # Secondary sort -span_id: bbbb before aaaa when started_at equal
    if row_a.started_at == row_b.started_at:
        assert rows[0].span_id == "bbbbbbbbbbbbbbbb"
    else:
        assert rows[0].started_at >= rows[1].started_at
