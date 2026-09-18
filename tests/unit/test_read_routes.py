"""Tests for GET read routes (definitions + saga instance list)."""

from contextlib import asynccontextmanager

import pytest
from common.models import SagaDefinition, SagaInstance, SagaStatus, SagaStepInstance, StepStatus
from engine.api.routes.definitions import router as definitions_router
from engine.api.routes.human_gate import router as human_gate_router
from engine.api.routes.sagas import router as sagas_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def read_app():
    """FastAPI app with read routers; Tortoise from tests/conftest autouse.

    Mount order matches production: HITL literals before parameterized saga paths.
    """

    @asynccontextmanager
    async def noop_lifespan(_: FastAPI):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(human_gate_router, prefix="/v1")
    app.include_router(sagas_router, prefix="/v1")
    app.include_router(definitions_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_get_definitions_saga_by_id_404(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/definitions/sagas",
            params={"id": "00000000-0000-4000-8000-000000000001"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "CATALOG_DEFINITION_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_definitions_saga_by_id_404_invalid_uuid(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas", params={"id": "not-a-uuid"})
    assert resp.status_code == 422


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
        resp = await client.get("/v1/definitions/sagas", params={"id": str(row.id)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "by-id"
    assert data["version"] == "2.0.0"
    assert data["id"] == str(row.id)


@pytest.mark.asyncio
async def test_get_definitions_worker_by_id_200_with_body(read_app):
    from common.models import WorkerDefinition
    from tests.factories import worker_definition_body

    row = await WorkerDefinition.create(
        namespace="default",
        name="by-id-worker",
        version="1.0.0",
        body=worker_definition_body(
            name="by-id-worker",
            provider="mock",
            model_name="demo",
            system_prompt="You are a test worker.",
            tool_sources=[{"name": "mock", "transport": "stdio", "command": "python"}],
        ),
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/definitions/workers",
            params={"id": str(row.id), "include_body": "true"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "by-id-worker"
    assert data["is_active"] is True
    assert "adapter" not in data
    assert data["body"]["kind"] == "worker"
    assert data["body"]["provider"] == "mock"
    assert data["body"]["adapter"] == "langchain"
    assert data["body"]["system_prompt"] == "You are a test worker."


@pytest.mark.asyncio
async def test_patch_definitions_worker_set_inactive(read_app):
    from common.models import WorkerDefinition
    from tests.factories import worker_definition_body

    row = await WorkerDefinition.create(
        namespace="default",
        name="toggle-worker",
        version="1.0.0",
        body=worker_definition_body(
            name="toggle-worker",
            provider="mock",
            model_name="demo",
            system_prompt="x",
        ),
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/v1/definitions/workers",
            params={"id": str(row.id)},
            json={"is_active": False},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_active"] is False
    await row.refresh_from_db()
    assert row.is_active is False


@pytest.mark.asyncio
async def test_patch_definitions_worker_by_triple(read_app):
    from common.models import WorkerDefinition
    from tests.factories import worker_definition_body

    row = await WorkerDefinition.create(
        namespace="default",
        name="triple-worker",
        version="2.0.0",
        body=worker_definition_body(
            name="triple-worker",
            provider="mock",
            model_name="demo",
            system_prompt="x",
        ),
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/v1/definitions/workers",
            params={
                "namespace": "default",
                "name": "triple-worker",
                "version": "2.0.0",
            },
            json={"is_active": False},
        )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    await row.refresh_from_db()
    assert row.is_active is False


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
    assert data["has_more"] is False


@pytest.mark.asyncio
async def test_get_definitions_sagas_limit_invalid(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas", params={"limit": 0})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    if isinstance(detail, list):
        assert any("limit" in str(item.get("msg", "")).lower() for item in detail)
    else:
        assert "limit" in detail.lower()


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
    if isinstance(detail, list):
        assert any("invalid" in str(item.get("msg", "")).lower() for item in detail)
    else:
        assert "Invalid" in detail or "invalid" in detail


@pytest.mark.asyncio
async def test_get_sagas_failed_and_status_conflict(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/sagas",
            params=[("failed", "true"), ("status", "COMPLETED")],
        )
    assert resp.status_code == 400
    assert "failed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_sagas_in_flight_and_failed_conflict(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/sagas",
            params=[("in_flight", "true"), ("failed", "true")],
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "in_flight" in detail or "failed" in detail


@pytest.mark.asyncio
async def test_get_sagas_exposes_definition_labels(read_app):
    await SagaInstance.create(
        trace_id="a" * 32,
        namespace="default",
        definition_id="def-labeled",
        definition_name="demo-saga",
        definition_version="2.0.0",
        status=SagaStatus.COMPLETED,
        context={},
    )
    await SagaInstance.create(
        trace_id="b" * 32,
        namespace="default",
        definition_id="def-orphan",
        status=SagaStatus.RUNNING,
        context={},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas")
    assert resp.status_code == 200
    by_trace = {it["trace_id"]: it for it in resp.json()["items"]}
    labeled = by_trace["a" * 32]
    assert labeled["definition_name"] == "demo-saga"
    assert labeled["definition_version"] == "2.0.0"
    assert labeled["definition_id"] == "def-labeled"
    orphan = by_trace["b" * 32]
    assert orphan["definition_name"] is None
    assert orphan["definition_version"] is None
    assert orphan["definition_id"] == "def-orphan"


@pytest.mark.asyncio
async def test_get_sagas_failed_filters(read_app):
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
        status=SagaStatus.FAILED,
        context={},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas", params={"failed": "true"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    trace_ids = {it["trace_id"] for it in items}
    assert trace_ids == {"b" * 32}


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
async def test_get_saga_by_path_200(read_app):
    trace_id = "a" * 32
    await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        definition_name="demo",
        definition_version="1.0.0",
        status=SagaStatus.RUNNING,
        context={},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{trace_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == trace_id
    assert body["status"] == "RUNNING"
    assert body["definition_name"] == "demo"
    assert "items" not in body


@pytest.mark.asyncio
async def test_get_saga_by_path_404(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{'b' * 32}")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "SAGA_INSTANCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_saga_by_path_invalid_trace_id(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas/not-a-trace-id")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_pending_review_not_shadowed_by_trace_id_path(read_app):
    """GET /pending-review must not be captured as GET /{trace_id}."""
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/sagas/pending-review")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "limit" in body


@pytest.mark.asyncio
async def test_get_saga_steps_by_path(read_app):
    trace_id = "c" * 32
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
        step_id="only",
        step_name="only",
        order_index=0,
        forward_seq=0,
        idempotency_key="k1",
        status=StepStatus.COMPLETED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{trace_id}/steps")
        # Literal /steps is not a collection route; treated as invalid trace_id.
        literal = await client.get("/v1/sagas/steps")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["step_id"] == "only"
    assert literal.status_code == 422


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
        forward_seq=1,
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
        forward_seq=0,
        idempotency_key="k1",
        status=StepStatus.COMPLETED,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{trace_id}/steps")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it["step_id"] for it in items] == ["first", "second"]
    assert items[0]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_get_saga_steps_not_found(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/sagas/{'f' * 32}/steps")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "SAGA_INSTANCE_NOT_FOUND"

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
            f"/v1/sagas/{trace_id}/steps",
            params=[("status", "IN_PROGRESS")],
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
        resp = await client.get(f"/v1/sagas/{trace_id}/steps")
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
        resp = await client.get(f"/v1/sagas/{trace_id}/steps")
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
        list_resp = await client.get(f"/v1/sagas/{trace_id}/steps")
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


@pytest.mark.asyncio
async def test_list_definitions_omits_null_body(read_app):
    await SagaDefinition.create(
        namespace="default",
        name="no-body",
        version="1.0.0",
        is_active=True,
        body={"steps": []},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas", params={"namespace": "default"})
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert "body" not in item


@pytest.mark.asyncio
async def test_get_definition_by_triple_query(read_app):
    row = await SagaDefinition.create(
        namespace="default",
        name="triple-get",
        version="3.0.0",
        is_active=True,
        body={"steps": [{"id": "a"}]},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/definitions/sagas",
            params={
                "namespace": "default",
                "name": "triple-get",
                "version": "3.0.0",
                "include_body": "true",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(row.id)
    assert data["name"] == "triple-get"
    assert data["body"]["steps"] == [{"id": "a"}]
    assert "items" not in data


@pytest.mark.asyncio
async def test_get_definition_by_id_query(read_app):
    row = await SagaDefinition.create(
        namespace="default",
        name="id-query",
        version="1.0.0",
        is_active=True,
        body={"steps": []},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/definitions/sagas", params={"id": str(row.id)})
    assert resp.status_code == 200
    assert resp.json()["name"] == "id-query"
    assert "body" not in resp.json()


@pytest.mark.asyncio
async def test_get_definition_partial_triple_400(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/definitions/sagas",
            params={"namespace": "default", "version": "1.0.0"},
        )
    assert resp.status_code == 400
    assert "triple" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_definition_by_triple_404_structured(read_app):
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/definitions/sagas",
            params={"namespace": "default", "name": "missing", "version": "9.9.9"},
        )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "CATALOG_DEFINITION_NOT_FOUND"
    assert detail["kind"] == "saga"
    assert detail["name"] == "missing"


@pytest.mark.asyncio
async def test_steps_and_pending_review_include_total(read_app):
    trace_id = "d" * 32
    saga = await SagaInstance.create(
        trace_id=trace_id,
        namespace="default",
        definition_id="def-1",
        status=SagaStatus.AWAITING_HUMAN,
        context={},
    )
    await SagaStepInstance.create(
        span_id="1111111111111111",
        saga=saga,
        saga_trace_id=trace_id,
        namespace="default",
        step_id="review-me",
        step_name="review-me",
        order_index=0,
        forward_seq=0,
        idempotency_key="k1",
        status=StepStatus.AWAITING_HUMAN,
        worker="w",
        worker_version="1.0.0",
        step_kind="reason",
        timeout_seconds=60,
        pending_review_payload={"x": 1},
    )
    transport = ASGITransport(app=read_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        steps = await client.get(
            f"/v1/sagas/{trace_id}/steps",
            params={"include_total": "true"},
        )
        pending = await client.get(
            "/v1/sagas/pending-review",
            params={"include_total": "true", "trace_id": trace_id},
        )
    assert steps.status_code == 200
    assert steps.json()["total"] == 1
    assert pending.status_code == 200
    assert pending.json()["total"] == 1
