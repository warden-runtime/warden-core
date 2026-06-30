"""Engine ↔ worker wiring via outbox with `resolve_adapter` mocked.

Run locally::

    uv run pytest tests/integration -vv --log-cli-level=DEBUG

Only integration-marked tests::

    uv run pytest -m integration -vv

Set breakpoints on `workers.logic.handle_worker_command` or the patched `run_step`.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from common.agent_adapter import ExecutionStepError
from common.contracts import HumanApprovedOutboxPayload, coerce_saga_ingest_dict
from common.models import (
    OutboxEvent,
    ProcessedCommand,
    ProcessedIngestEvent,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
)
from engine.api.saga_start import start_saga
from engine.logic import process_saga_event
from workers.logic import handle_worker_command

INTEGRATION_WORKER = "integration-worker"
INTEGRATION_WORKER_VERSION = "1.0.0"
INTEGRATION_SAGA_NAME = "integration-two-step"
INTEGRATION_SAGA_VERSION = "1.0.0"
INTEGRATION_ONE_STEP_FAIL_NAME = "integration-one-step-fail"
INTEGRATION_ONE_STEP_FAIL_VERSION = "1.0.0"
INTEGRATION_HITL_SAGA_NAME = "integration-hitl-two-step"
INTEGRATION_HITL_SAGA_VERSION = "1.0.0"
INTEGRATION_COMP_SAGA_NAME = "integration-comp-two-step"
INTEGRATION_COMP_SAGA_VERSION = "1.0.0"
INTEGRATION_HITL_ONE_STEP_NAME = "integration-hitl-one-step"
INTEGRATION_HITL_ONE_STEP_VERSION = "1.0.0"
INTEGRATION_DIRTY_COMP_SAGA_NAME = "integration-dirty-comp-two-step"
INTEGRATION_DIRTY_COMP_SAGA_VERSION = "1.0.0"

TWO_STEP_SAGA_BODY = {
    "kind": "saga",
    "name": INTEGRATION_SAGA_NAME,
    "namespace": "default",
    "version": INTEGRATION_SAGA_VERSION,
    "description": "Integration wiring test",
    "steps": [
        {
            "id": "step-1",
            "kind": "reason",
            "name": "First step",
            "worker": INTEGRATION_WORKER,
            "worker_version": INTEGRATION_WORKER_VERSION,
            "with": {"message": {"value": "integration step one"}},
            "prompt": "generic-step.j2",
            "tools": {"allow": []},
            "timeout_seconds": 600,
        },
        {
            "id": "step-2",
            "kind": "reason",
            "name": "Second step",
            "worker": INTEGRATION_WORKER,
            "worker_version": INTEGRATION_WORKER_VERSION,
            "with": {"message": {"value": "integration step two"}},
            "prompt": "generic-step.j2",
            "tools": {"allow": []},
            "timeout_seconds": 600,
        },
    ],
}

ONE_STEP_FAIL_SAGA_BODY = {
    "kind": "saga",
    "name": INTEGRATION_ONE_STEP_FAIL_NAME,
    "namespace": "default",
    "version": INTEGRATION_ONE_STEP_FAIL_VERSION,
    "description": "Integration failure-path test",
    "steps": [
        {
            "id": "only-step",
            "kind": "reason",
            "name": "Only step",
            "worker": INTEGRATION_WORKER,
            "worker_version": INTEGRATION_WORKER_VERSION,
            "with": {"message": {"value": "integration failure path"}},
            "prompt": "generic-step.j2",
            "tools": {"allow": []},
            "timeout_seconds": 600,
        }
    ],
}

HITL_TWO_STEP_SAGA_BODY = {
    "kind": "saga",
    "name": INTEGRATION_HITL_SAGA_NAME,
    "namespace": "default",
    "version": INTEGRATION_HITL_SAGA_VERSION,
    "description": "HITL hold then approve",
    "steps": [
        {
            "id": "step-1",
            "kind": "reason",
            "name": "Review gate",
            "worker": INTEGRATION_WORKER,
            "worker_version": INTEGRATION_WORKER_VERSION,
            "with": {"message": {"value": "needs review"}},
            "prompt": "generic-step.j2",
            "tools": {"allow": []},
            "timeout_seconds": 600,
            "hitl": True,
            "hitl_retry_guidance": "fix the summary before resubmitting",
        },
        {
            "id": "step-2",
            "kind": "reason",
            "name": "After approval",
            "worker": INTEGRATION_WORKER,
            "worker_version": INTEGRATION_WORKER_VERSION,
            "with": {"message": {"value": "post hitl"}},
            "prompt": "generic-step.j2",
            "tools": {"allow": []},
            "timeout_seconds": 600,
        },
    ],
}

HITL_ONE_STEP_SAGA_BODY = {
    "kind": "saga",
    "name": INTEGRATION_HITL_ONE_STEP_NAME,
    "namespace": "default",
    "version": INTEGRATION_HITL_ONE_STEP_VERSION,
    "description": "Single HITL gate",
    "steps": [
        {
            "id": "only-step",
            "kind": "reason",
            "name": "Review only",
            "worker": INTEGRATION_WORKER,
            "worker_version": INTEGRATION_WORKER_VERSION,
            "with": {"message": {"value": "reject path"}},
            "prompt": "generic-step.j2",
            "tools": {"allow": []},
            "timeout_seconds": 600,
            "hitl": True,
        },
    ],
}


def _reason_step(*, step_id: str, name: str, compensation: str | None = None) -> dict:
    body: dict = {
        "id": step_id,
        "kind": "reason",
        "name": name,
        "worker": INTEGRATION_WORKER,
        "worker_version": INTEGRATION_WORKER_VERSION,
        "with": {"message": {"value": name}},
        "prompt": "generic-step.j2",
        "tools": {"allow": []},
        "timeout_seconds": 600,
    }
    if compensation is not None:
        body["compensation"] = compensation
    return body


def compensating_two_step_saga_body(*, compensation_ref: str) -> dict:
    return {
        "kind": "saga",
        "name": INTEGRATION_COMP_SAGA_NAME,
        "namespace": "default",
        "version": INTEGRATION_COMP_SAGA_VERSION,
        "description": "Step-two clean failure rolls back step one",
        "steps": [
            _reason_step(step_id="step-1", name="First step", compensation=compensation_ref),
            _reason_step(step_id="step-2", name="Second step"),
        ],
    }


def dirty_compensating_two_step_saga_body(*, compensation_ref: str) -> dict:
    """Both steps carry compensation: dirty failure on step two undoes itself, then step one."""
    return {
        "kind": "saga",
        "name": INTEGRATION_DIRTY_COMP_SAGA_NAME,
        "namespace": "default",
        "version": INTEGRATION_DIRTY_COMP_SAGA_VERSION,
        "description": "Step-two timeout compensates self then prior step",
        "steps": [
            _reason_step(step_id="step-1", name="First step", compensation=compensation_ref),
            _reason_step(step_id="step-2", name="Second step", compensation=compensation_ref),
        ],
    }


async def latest_outbox(
    trace_id: str,
    event_type: str,
    *,
    step_span_id: str | None = None,
) -> OutboxEvent | None:
    """Most recent outbox row for ``trace_id`` and ``event_type``, optional ``step_span_id``."""
    q = OutboxEvent.filter(saga_trace_id=trace_id, event_type=event_type)
    if step_span_id is not None:
        q = q.filter(step_span_id=step_span_id)
    return await q.order_by("-created_at").first()


def do_step_dict_from_outbox(evt: OutboxEvent) -> dict:
    """Command dict for ``handle_worker_command`` (matches ``DoStepCommand.model_dump``)."""
    return dict(evt.payload)


def step_completed_ingest_from_outbox(evt: OutboxEvent) -> dict:
    """Ingest dict for ``process_saga_event`` (``StepCompletedIngestEvent`` shape)."""
    p = evt.payload
    out = {
        "event_type": "STEP_COMPLETED",
        "saga_trace_id": p["saga_trace_id"],
        "namespace": p["namespace"],
        "step_span_id": p["step_span_id"],
        "output": p["output"],
    }
    if p.get("timing") is not None:
        out["timing"] = p["timing"]
    return out


def step_failed_ingest_from_outbox(evt: OutboxEvent) -> dict:
    """Ingest dict for ``process_saga_event`` (``StepFailedEvent`` / unit-test shapes)."""
    p = evt.payload
    err = p.get("error_details")
    if err is None:
        err = p.get("output")
    out = {
        "event_type": "STEP_FAILED",
        "saga_trace_id": p["saga_trace_id"],
        "namespace": p["namespace"],
        "step_span_id": p["step_span_id"],
        "error_details": err,
        "output": p.get("output"),
    }
    if p.get("timing") is not None:
        out["timing"] = p["timing"]
    return out


def step_compensated_ingest_from_outbox(evt: OutboxEvent) -> dict:
    """Ingest dict for ``process_saga_event`` (``StepCompensatedIngestEvent`` shape)."""
    p = evt.payload
    out = {
        "event_type": "STEP_COMPENSATED",
        "saga_trace_id": p["saga_trace_id"],
        "namespace": p["namespace"],
        "step_span_id": p["step_span_id"],
        "output": p.get("output"),
    }
    if p.get("timing") is not None:
        out["timing"] = p["timing"]
    return out


async def bootstrap_running_saga(
    *,
    namespace: str,
    name: str,
    version: str,
) -> tuple[str, list[SagaStepInstance]]:
    """``start_saga`` plus ``SAGA_STARTED`` ingest; returns trace id and ordered steps."""
    trace_id = await start_saga(namespace=namespace, name=name, version=version, input={})
    await process_saga_event(
        {
            "event_type": "SAGA_STARTED",
            "saga_trace_id": trace_id,
            "namespace": namespace,
            "step_span_id": None,
        }
    )
    steps = await SagaStepInstance.filter(saga_trace_id=trace_id).order_by("order_index")
    return trace_id, list(steps)


def patch_successful_run_step(mocker, *, outputs: list[dict] | None = None) -> MagicMock:
    """Mock adapter ``run_step`` to return sequential ``outputs`` (default one success)."""
    fake_adapter = MagicMock()
    payloads = outputs or [{"data": {"summary": "ok", "done": True}}]
    call_n = {"n": 0}

    async def _run_step(**_kwargs: object) -> MagicMock:
        idx = min(call_n["n"], len(payloads) - 1)
        call_n["n"] += 1
        out = MagicMock()
        out.output = payloads[idx]
        return out

    fake_adapter.run_step = AsyncMock(side_effect=_run_step)
    mocker.patch("workers.logic.resolve_adapter", return_value=fake_adapter)
    return fake_adapter


async def worker_completes_do_step(trace_id: str, do_evt: OutboxEvent) -> OutboxEvent:
    await handle_worker_command(do_step_dict_from_outbox(do_evt))
    sc = await latest_outbox(trace_id, "STEP_COMPLETED", step_span_id=do_evt.step_span_id)
    assert sc is not None
    return sc


async def engine_ingests_step_completed(sc_evt: OutboxEvent) -> None:
    await process_saga_event(step_completed_ingest_from_outbox(sc_evt))


async def worker_runs_compensation_and_ingest(trace_id: str, undo: SagaStepInstance) -> None:
    comp_cmd = await latest_outbox(trace_id, "EXECUTE_COMPENSATION", step_span_id=undo.span_id)
    assert comp_cmd is not None
    await handle_worker_command(do_step_dict_from_outbox(comp_cmd))
    compensated_evt = await latest_outbox(trace_id, "STEP_COMPENSATED", step_span_id=undo.span_id)
    assert compensated_evt is not None
    await process_saga_event(step_compensated_ingest_from_outbox(compensated_evt))


@pytest_asyncio.fixture
async def integration_worker() -> str:
    """Worker definition + provider secret for integration tests."""
    from common.models import ProviderSecret, WorkerDefinition

    await WorkerDefinition.create(
        namespace="default",
        name=INTEGRATION_WORKER,
        version=INTEGRATION_WORKER_VERSION,
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="You are a test assistant.",
    )
    await ProviderSecret.create(
        id=uuid4(),
        namespace="default",
        provider="openai",
        api_key="sk-fake",
    )
    return INTEGRATION_WORKER


@pytest_asyncio.fixture
async def two_step_saga_definition(integration_worker: str) -> None:
    from common.models import SagaDefinition

    await SagaDefinition.create(
        namespace="default",
        name=INTEGRATION_SAGA_NAME,
        version=INTEGRATION_SAGA_VERSION,
        body=TWO_STEP_SAGA_BODY,
    )


@pytest_asyncio.fixture
async def one_step_fail_saga_definition(integration_worker: str) -> None:
    from common.models import SagaDefinition

    await SagaDefinition.create(
        namespace="default",
        name=INTEGRATION_ONE_STEP_FAIL_NAME,
        version=INTEGRATION_ONE_STEP_FAIL_VERSION,
        body=ONE_STEP_FAIL_SAGA_BODY,
    )


@pytest_asyncio.fixture
async def hitl_two_step_saga_definition(integration_worker: str) -> None:
    from common.models import SagaDefinition

    await SagaDefinition.create(
        namespace="default",
        name=INTEGRATION_HITL_SAGA_NAME,
        version=INTEGRATION_HITL_SAGA_VERSION,
        body=HITL_TWO_STEP_SAGA_BODY,
    )


@pytest_asyncio.fixture
async def compensating_two_step_saga_definition(
    integration_worker: str,
    tmp_path,
    monkeypatch,
) -> None:
    from common.config import get_settings
    from common.models import SagaDefinition

    (tmp_path / "undo.yaml").write_text(
        "worker: integration-worker\nworker_version: 1.0.0\nwith: {}\ntools:\n  allow: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHEMAS_ROOT", str(tmp_path))
    monkeypatch.setenv("COMPENSATIONS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    await SagaDefinition.create(
        namespace="default",
        name=INTEGRATION_COMP_SAGA_NAME,
        version=INTEGRATION_COMP_SAGA_VERSION,
        body=compensating_two_step_saga_body(compensation_ref="undo.yaml"),
    )
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def hitl_one_step_saga_definition(integration_worker: str) -> None:
    from common.models import SagaDefinition

    await SagaDefinition.create(
        namespace="default",
        name=INTEGRATION_HITL_ONE_STEP_NAME,
        version=INTEGRATION_HITL_ONE_STEP_VERSION,
        body=HITL_ONE_STEP_SAGA_BODY,
    )


@pytest_asyncio.fixture
async def dirty_compensating_two_step_saga_definition(
    integration_worker: str,
    tmp_path,
    monkeypatch,
) -> None:
    from common.config import get_settings
    from common.models import SagaDefinition

    (tmp_path / "undo.yaml").write_text(
        "worker: integration-worker\nworker_version: 1.0.0\nwith: {}\ntools:\n  allow: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHEMAS_ROOT", str(tmp_path))
    monkeypatch.setenv("COMPENSATIONS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    await SagaDefinition.create(
        namespace="default",
        name=INTEGRATION_DIRTY_COMP_SAGA_NAME,
        version=INTEGRATION_DIRTY_COMP_SAGA_VERSION,
        body=dirty_compensating_two_step_saga_body(compensation_ref="undo.yaml"),
    )
    yield
    get_settings.cache_clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_step_reason_saga_happy_path(
    mocker,
    two_step_saga_definition: None,
) -> None:
    """start_saga → SAGA_STARTED → DO_STEP → mocked worker → STEP_COMPLETED → … → COMPLETED."""
    trace_id = await start_saga(
        namespace="default",
        name=INTEGRATION_SAGA_NAME,
        version=INTEGRATION_SAGA_VERSION,
        input={},
    )

    await process_saga_event(
        {
            "event_type": "SAGA_STARTED",
            "saga_trace_id": trace_id,
            "namespace": "default",
            "step_span_id": None,
        }
    )

    steps = await SagaStepInstance.filter(saga_trace_id=trace_id).order_by("order_index")
    assert len(steps) == 2
    step0, step1 = steps[0], steps[1]

    saga = await SagaInstance.get(trace_id=trace_id)
    assert saga.status == SagaStatus.RUNNING
    assert step0.status == StepStatus.IN_PROGRESS

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    assert do0.payload.get("type") == "DO_STEP"

    fake_adapter = MagicMock()
    call_n = {"n": 0}

    async def _run_step(**_kwargs: object) -> MagicMock:
        call_n["n"] += 1
        out = MagicMock()
        out.output = {"data": {"summary": f"step{call_n['n']}", "done": True}}
        return out

    fake_adapter.run_step = AsyncMock(side_effect=_run_step)
    mocker.patch("workers.logic.resolve_adapter", return_value=fake_adapter)

    await handle_worker_command(do_step_dict_from_outbox(do0))

    sc0 = await latest_outbox(trace_id, "STEP_COMPLETED", step_span_id=step0.span_id)
    assert sc0 is not None

    await process_saga_event(step_completed_ingest_from_outbox(sc0))

    await step0.refresh_from_db()
    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step0.status == StepStatus.COMPLETED
    assert step1.status == StepStatus.IN_PROGRESS

    do1 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step1.span_id)
    assert do1 is not None

    await handle_worker_command(do_step_dict_from_outbox(do1))

    sc1 = await latest_outbox(trace_id, "STEP_COMPLETED", step_span_id=step1.span_id)
    assert sc1 is not None

    await process_saga_event(step_completed_ingest_from_outbox(sc1))

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    await step1.refresh_from_db()
    assert saga.status == SagaStatus.COMPLETED
    assert step0.status == StepStatus.COMPLETED
    assert step1.status == StepStatus.COMPLETED

    done = await latest_outbox(trace_id, "SAGA_COMPLETED")
    assert done is not None
    assert done.payload.get("status") == "SAGA_COMPLETED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_step_reason_saga_worker_failure_emits_saga_failed(
    mocker,
    one_step_fail_saga_definition: None,
) -> None:
    """DO_STEP → mocked ``run_step`` raises → STEP_FAILED outbox → engine marks saga FAILED."""
    trace_id = await start_saga(
        namespace="default",
        name=INTEGRATION_ONE_STEP_FAIL_NAME,
        version=INTEGRATION_ONE_STEP_FAIL_VERSION,
        input={},
    )

    await process_saga_event(
        {
            "event_type": "SAGA_STARTED",
            "saga_trace_id": trace_id,
            "namespace": "default",
            "step_span_id": None,
        }
    )

    step0 = await SagaStepInstance.filter(saga_trace_id=trace_id).first()
    assert step0 is not None
    saga = await SagaInstance.get(trace_id=trace_id)
    assert step0.status == StepStatus.IN_PROGRESS

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None

    fake_adapter = MagicMock()
    fake_adapter.run_step = AsyncMock(
        side_effect=ExecutionStepError(
            "adapter rejected",
            error_details={"error": "Validation Failed", "code": "BAD_REQUEST"},
        )
    )
    mocker.patch("workers.logic.resolve_adapter", return_value=fake_adapter)

    await handle_worker_command(do_step_dict_from_outbox(do0))

    failed_evt = await latest_outbox(trace_id, "STEP_FAILED", step_span_id=step0.span_id)
    assert failed_evt is not None

    await process_saga_event(step_failed_ingest_from_outbox(failed_evt))

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    assert step0.status == StepStatus.FAILED
    assert saga.status == SagaStatus.FAILED

    saga_failed = await latest_outbox(trace_id, "SAGA_FAILED")
    assert saga_failed is not None
    assert saga_failed.payload.get("status") == "SAGA_FAILED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hitl_reason_step_hold_approve_then_completes(
    mocker,
    hitl_two_step_saga_definition: None,
) -> None:
    """Worker output held for HITL; approval resumes saga and second step completes."""
    trace_id, (step0, step1) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_HITL_SAGA_NAME,
        version=INTEGRATION_HITL_SAGA_VERSION,
    )
    saga = await SagaInstance.get(trace_id=trace_id)
    patch_successful_run_step(
        mocker,
        outputs=[
            {"data": {"draft": True, "summary": "awaiting review"}},
            {"data": {"summary": "final", "done": True}},
        ],
    )

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    sc0 = await worker_completes_do_step(trace_id, do0)
    await engine_ingests_step_completed(sc0)

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    await step1.refresh_from_db()
    assert saga.status == SagaStatus.AWAITING_HUMAN
    assert step0.status == StepStatus.AWAITING_HUMAN
    assert step0.pending_review_payload == {"data": {"draft": True, "summary": "awaiting review"}}
    assert step1.status == StepStatus.PENDING
    assert await latest_outbox(trace_id, "DO_STEP", step_span_id=step1.span_id) is None

    await process_saga_event(
        {
            "event_type": "HUMAN_APPROVED",
            "saga_trace_id": trace_id,
            "namespace": "default",
            "step_span_id": step0.span_id,
        }
    )

    await step0.refresh_from_db()
    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step0.status == StepStatus.COMPLETED
    assert step1.status == StepStatus.IN_PROGRESS
    assert saga.status == SagaStatus.RUNNING

    do1 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step1.span_id)
    assert do1 is not None
    sc1 = await worker_completes_do_step(trace_id, do1)
    await engine_ingests_step_completed(sc1)

    await saga.refresh_from_db()
    assert saga.status == SagaStatus.COMPLETED
    done = await latest_outbox(trace_id, "SAGA_COMPLETED")
    assert done is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hitl_approve_outbox_wire_payload_advances_saga(
    mocker,
    hitl_two_step_saga_definition: None,
) -> None:
    """Full HumanApprovedOutboxPayload wire dict (incl. idempotency_key) must ingest and advance."""
    trace_id, (step0, step1) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_HITL_SAGA_NAME,
        version=INTEGRATION_HITL_SAGA_VERSION,
    )
    saga = await SagaInstance.get(trace_id=trace_id)
    patch_successful_run_step(
        mocker,
        outputs=[
            {"data": {"draft": True, "summary": "awaiting review"}},
            {"data": {"summary": "final", "done": True}},
        ],
    )

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    sc0 = await worker_completes_do_step(trace_id, do0)
    await engine_ingests_step_completed(sc0)

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    assert saga.status == SagaStatus.AWAITING_HUMAN
    assert step0.status == StepStatus.AWAITING_HUMAN

    wire = HumanApprovedOutboxPayload(
        namespace="default",
        saga_trace_id=trace_id,
        step_span_id=step0.span_id,
        idempotency_key=f"human-decision-{trace_id}-{step0.span_id}",
        output=None,
    ).model_dump(mode="json")
    wire["event_type"] = "HUMAN_APPROVED"

    await process_saga_event(coerce_saga_ingest_dict(wire))

    await step0.refresh_from_db()
    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step0.status == StepStatus.COMPLETED
    assert step1.status == StepStatus.IN_PROGRESS
    assert saga.status == SagaStatus.RUNNING


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_step_clean_failure_compensates_via_worker(
    mocker,
    compensating_two_step_saga_definition: None,
) -> None:
    """Step-two clean failure triggers EXECUTE_COMPENSATION; worker undo completes saga."""
    trace_id, (step0, step1) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_COMP_SAGA_NAME,
        version=INTEGRATION_COMP_SAGA_VERSION,
    )
    saga = await SagaInstance.get(trace_id=trace_id)

    fake_adapter = patch_successful_run_step(mocker)
    fake_adapter.run_compensation = AsyncMock(
        return_value=MagicMock(output={"refund_id": "rf-1", "data": {"status": "voided"}}),
    )

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    sc0 = await worker_completes_do_step(trace_id, do0)
    await engine_ingests_step_completed(sc0)

    do1 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step1.span_id)
    assert do1 is not None
    fake_adapter.run_step = AsyncMock(
        side_effect=ExecutionStepError(
            "adapter rejected",
            error_details={"error": "Validation Failed", "code": "BAD_REQUEST"},
        )
    )

    await handle_worker_command(do_step_dict_from_outbox(do1))
    failed_evt = await latest_outbox(trace_id, "STEP_FAILED", step_span_id=step1.span_id)
    assert failed_evt is not None
    await process_saga_event(step_failed_ingest_from_outbox(failed_evt))

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    await step1.refresh_from_db()
    assert step1.status == StepStatus.FAILED
    assert step0.status == StepStatus.COMPLETED
    assert saga.status == SagaStatus.COMPENSATING

    undo0 = await SagaStepInstance.filter(
        saga_trace_id=trace_id,
        compensates_span_id=step0.span_id,
    ).first()
    assert undo0 is not None
    assert undo0.status == StepStatus.COMPENSATING

    comp_cmd = await latest_outbox(trace_id, "EXECUTE_COMPENSATION", step_span_id=undo0.span_id)
    assert comp_cmd is not None

    await handle_worker_command(do_step_dict_from_outbox(comp_cmd))
    compensated_evt = await latest_outbox(trace_id, "STEP_COMPENSATED", step_span_id=undo0.span_id)
    assert compensated_evt is not None
    await process_saga_event(step_compensated_ingest_from_outbox(compensated_evt))

    await saga.refresh_from_db()
    await undo0.refresh_from_db()
    assert undo0.status == StepStatus.COMPENSATED
    assert saga.status == SagaStatus.COMPENSATED
    saga_compensated = await latest_outbox(trace_id, "SAGA_COMPENSATED")
    assert saga_compensated is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_do_step_delivery_is_idempotent(
    mocker,
    two_step_saga_definition: None,
) -> None:
    """Redelivered DO_STEP with the same idempotency key does not re-run the adapter."""
    trace_id, (step0, _step1) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_SAGA_NAME,
        version=INTEGRATION_SAGA_VERSION,
    )
    fake_adapter = patch_successful_run_step(mocker)

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    cmd = do_step_dict_from_outbox(do0)

    await handle_worker_command(cmd)
    await handle_worker_command(cmd)

    assert fake_adapter.run_step.await_count == 1
    completed_count = await OutboxEvent.filter(
        saga_trace_id=trace_id,
        event_type="STEP_COMPLETED",
        step_span_id=step0.span_id,
    ).count()
    assert completed_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hitl_retry_requeues_worker_then_completes_after_approval(
    mocker,
    hitl_two_step_saga_definition: None,
) -> None:
    """HUMAN_RETRY releases prior claim, re-runs worker with guidance, then approve finishes saga."""
    trace_id, (step0, step1) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_HITL_SAGA_NAME,
        version=INTEGRATION_HITL_SAGA_VERSION,
    )
    saga = await SagaInstance.get(trace_id=trace_id)
    fake_adapter = patch_successful_run_step(
        mocker,
        outputs=[
            {"data": {"draft": True, "summary": "first pass"}},
            {"data": {"draft": True, "summary": "second pass"}},
            {"data": {"summary": "final", "done": True}},
        ],
    )

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    sc0 = await worker_completes_do_step(trace_id, do0)
    await engine_ingests_step_completed(sc0)

    await step0.refresh_from_db()
    prior_key = step0.idempotency_key
    assert step0.status == StepStatus.AWAITING_HUMAN
    assert await ProcessedCommand.filter(idempotency_key=prior_key).exists()

    await process_saga_event(
        {
            "event_type": "HUMAN_RETRY",
            "saga_trace_id": trace_id,
            "namespace": "default",
            "step_span_id": step0.span_id,
            "retry_guidance": "please shorten the summary",
        }
    )

    await step0.refresh_from_db()
    await saga.refresh_from_db()
    assert saga.status == SagaStatus.RUNNING
    assert step0.status == StepStatus.IN_PROGRESS
    assert step0.hitl_retry_count == 1
    assert step0.idempotency_key != prior_key
    assert not await ProcessedCommand.filter(idempotency_key=prior_key).exists()

    retry_do = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert retry_do is not None
    assert retry_do.payload.get("idempotency_key") == step0.idempotency_key
    hitl_ctx = retry_do.payload.get("arguments", {}).get("_hitl_retry", {})
    assert hitl_ctx.get("attempt") == 1
    assert hitl_ctx.get("guidance") == "please shorten the summary"

    sc_retry = await worker_completes_do_step(trace_id, retry_do)
    await engine_ingests_step_completed(sc_retry)

    await step0.refresh_from_db()
    await saga.refresh_from_db()
    assert step0.status == StepStatus.AWAITING_HUMAN
    assert saga.status == SagaStatus.AWAITING_HUMAN

    await process_saga_event(
        {
            "event_type": "HUMAN_APPROVED",
            "saga_trace_id": trace_id,
            "namespace": "default",
            "step_span_id": step0.span_id,
        }
    )

    do1 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step1.span_id)
    assert do1 is not None
    sc1 = await worker_completes_do_step(trace_id, do1)
    await engine_ingests_step_completed(sc1)

    await saga.refresh_from_db()
    assert saga.status == SagaStatus.COMPLETED
    assert fake_adapter.run_step.await_count == 3


async def _worker_command_count(trace_id: str) -> int:
    return await OutboxEvent.filter(
        saga_trace_id=trace_id,
        event_type__in=["DO_STEP", "DO_COMMIT"],
    ).count()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hitl_approve_and_retry_race_exactly_one_side_effect(
    mocker,
    hitl_two_step_saga_definition: None,
) -> None:
    """Concurrent HUMAN_APPROVED and HUMAN_RETRY: one wins, the other is dropped at status guard."""
    trace_id, (step0, step1) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_HITL_SAGA_NAME,
        version=INTEGRATION_HITL_SAGA_VERSION,
    )
    saga = await SagaInstance.get(trace_id=trace_id)
    patch_successful_run_step(
        mocker,
        outputs=[{"data": {"draft": True, "summary": "awaiting review"}}],
    )

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    sc0 = await worker_completes_do_step(trace_id, do0)
    await engine_ingests_step_completed(sc0)

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    await step1.refresh_from_db()
    assert saga.status == SagaStatus.AWAITING_HUMAN
    assert step0.status == StepStatus.AWAITING_HUMAN
    assert step1.status == StepStatus.PENDING

    worker_cmds_before = await _worker_command_count(trace_id)
    assert worker_cmds_before == 1

    approved_payload = {
        "event_type": "HUMAN_APPROVED",
        "saga_trace_id": trace_id,
        "namespace": "default",
        "step_span_id": step0.span_id,
    }
    retry_payload = {
        "event_type": "HUMAN_RETRY",
        "saga_trace_id": trace_id,
        "namespace": "default",
        "step_span_id": step0.span_id,
        "retry_guidance": "please shorten the summary",
    }
    results = await asyncio.gather(
        process_saga_event(approved_payload),
        process_saga_event(retry_payload),
        return_exceptions=True,
    )
    assert all(r is None for r in results)

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    await step1.refresh_from_db()

    approve_won = step0.status == StepStatus.COMPLETED
    retry_won = step0.status == StepStatus.IN_PROGRESS and step0.hitl_retry_count == 1
    assert approve_won ^ retry_won

    assert await _worker_command_count(trace_id) == worker_cmds_before + 1

    if approve_won:
        assert saga.status == SagaStatus.RUNNING
        assert step1.status == StepStatus.IN_PROGRESS
        assert await latest_outbox(trace_id, "DO_STEP", step_span_id=step1.span_id) is not None
        step0_do = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
        assert step0_do is not None
        assert step0_do.id == do0.id
        assert step0.hitl_retry_count == 0
    else:
        assert saga.status == SagaStatus.RUNNING
        assert step1.status == StepStatus.PENDING
        retry_do = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
        assert retry_do is not None
        assert retry_do.id != do0.id
        assert retry_do.payload.get("idempotency_key") == step0.idempotency_key

    assert await ProcessedIngestEvent.filter(
        event_dedup_key=f"{trace_id}:HUMAN_APPROVED:{step0.span_id}",
    ).exists()
    assert await ProcessedIngestEvent.filter(
        event_dedup_key=f"{trace_id}:HUMAN_RETRY:{step0.span_id}",
    ).exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hitl_reject_fails_saga_without_compensation(
    mocker,
    hitl_one_step_saga_definition: None,
) -> None:
    """HUMAN_REJECTED on the first step fails the saga cleanly with no rollback."""
    trace_id, (step0,) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_HITL_ONE_STEP_NAME,
        version=INTEGRATION_HITL_ONE_STEP_VERSION,
    )
    saga = await SagaInstance.get(trace_id=trace_id)
    patch_successful_run_step(mocker, outputs=[{"data": {"summary": "review me"}}])

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    sc0 = await worker_completes_do_step(trace_id, do0)
    await engine_ingests_step_completed(sc0)

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    assert saga.status == SagaStatus.AWAITING_HUMAN
    assert step0.status == StepStatus.AWAITING_HUMAN

    await process_saga_event(
        {
            "event_type": "HUMAN_REJECTED",
            "saga_trace_id": trace_id,
            "namespace": "default",
            "step_span_id": step0.span_id,
            "error_details": {"code": "HUMAN_REJECTED", "message": "not acceptable"},
        }
    )

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    assert saga.status == SagaStatus.FAILED
    assert step0.status == StepStatus.FAILED
    assert step0.error_details.get("code") == "HUMAN_REJECTED"
    assert await latest_outbox(trace_id, "SAGA_FAILED") is not None
    assert await latest_outbox(trace_id, "EXECUTE_COMPENSATION") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dirty_failure_compensates_self_then_prior_step(
    mocker,
    dirty_compensating_two_step_saga_definition: None,
) -> None:
    """Step-two timeout undoes itself first, then rolls back step one via worker."""
    trace_id, (step0, step1) = await bootstrap_running_saga(
        namespace="default",
        name=INTEGRATION_DIRTY_COMP_SAGA_NAME,
        version=INTEGRATION_DIRTY_COMP_SAGA_VERSION,
    )
    saga = await SagaInstance.get(trace_id=trace_id)

    fake_adapter = patch_successful_run_step(mocker)
    fake_adapter.run_compensation = AsyncMock(
        return_value=MagicMock(output={"refund_id": "rf-dirty", "data": {"status": "voided"}}),
    )

    do0 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step0.span_id)
    assert do0 is not None
    sc0 = await worker_completes_do_step(trace_id, do0)
    await engine_ingests_step_completed(sc0)

    do1 = await latest_outbox(trace_id, "DO_STEP", step_span_id=step1.span_id)
    assert do1 is not None
    fake_adapter.run_step = AsyncMock(
        side_effect=ExecutionStepError(
            "connection timeout",
            error_details={"error": "Connection timeout", "code": "TIMEOUT"},
        )
    )
    await handle_worker_command(do_step_dict_from_outbox(do1))
    failed_evt = await latest_outbox(trace_id, "STEP_FAILED", step_span_id=step1.span_id)
    assert failed_evt is not None
    await process_saga_event(step_failed_ingest_from_outbox(failed_evt))

    await step1.refresh_from_db()
    await step0.refresh_from_db()
    await saga.refresh_from_db()
    assert step1.status == StepStatus.TIMED_OUT
    assert step0.status == StepStatus.COMPLETED
    assert saga.status == SagaStatus.COMPENSATING

    undo1 = await SagaStepInstance.filter(
        saga_trace_id=trace_id,
        compensates_span_id=step1.span_id,
    ).first()
    assert undo1 is not None
    assert undo1.status == StepStatus.COMPENSATING

    await worker_runs_compensation_and_ingest(trace_id, undo1)

    await undo1.refresh_from_db()
    await step1.refresh_from_db()
    await step0.refresh_from_db()
    assert undo1.status == StepStatus.COMPENSATED
    assert step1.status == StepStatus.TIMED_OUT
    assert step0.status == StepStatus.COMPLETED

    undo0 = await SagaStepInstance.filter(
        saga_trace_id=trace_id,
        compensates_span_id=step0.span_id,
    ).first()
    assert undo0 is not None
    assert undo0.status == StepStatus.COMPENSATING

    await worker_runs_compensation_and_ingest(trace_id, undo0)

    await saga.refresh_from_db()
    await undo0.refresh_from_db()
    assert undo0.status == StepStatus.COMPENSATED
    assert saga.status == SagaStatus.COMPENSATED
    assert await latest_outbox(trace_id, "SAGA_COMPENSATED") is not None
    assert fake_adapter.run_compensation.await_count == 2
