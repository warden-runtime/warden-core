"""Regression tests for loop follow-up patches (until status, SKIPPED latest-wins, exhaust)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from common.loops import LOOP_STATUS_EXHAUSTED, PolicyEvaluationError
from common.models import (
    SagaDefinition,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
    WorkerDefinition,
)
from common.step_output import step_context_entry_for_saga
from engine.api.saga_start import start_saga
from engine.logic import _mark_step_skipped_when_false, process_saga_event
from tests.factories import create_saga_with_steps

_WORKER = "loop-followup-worker"
_SAGA_NAME = "loop-followup"
_SAGA_VERSION = "1.0.0"


def _reason_step(step_id: str, *, when_cel: str | None = None) -> dict:
    step: dict = {
        "id": step_id,
        "kind": "reason",
        "name": step_id.title(),
        "worker": _WORKER,
        "worker_version": "1.0.0",
        "agent-adapter": "simple",
        "with": {},
        "prompt": "noop.j2",
        "tools": {"allow": []},
        "timeout_seconds": 600,
    }
    if when_cel is not None:
        step["when"] = {"cel": when_cel}
    return step


def _saga_body(*, max_iterations: int, until_cel: str, body_steps: list[dict]) -> dict:
    return {
        "kind": "saga",
        "name": _SAGA_NAME,
        "namespace": "default",
        "version": _SAGA_VERSION,
        "description": "Loop follow-up fixtures",
        "steps": [
            {
                "id": "refine",
                "kind": "loop",
                "max_iterations": max_iterations,
                "until": {"cel": until_cel},
                "steps": body_steps,
            }
        ],
    }


async def _seed_worker_and_saga(body: dict) -> None:
    await WorkerDefinition.create(
        namespace="default",
        name=_WORKER,
        version="1.0.0",
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="Hi.",
    )
    await SagaDefinition.create(
        namespace="default",
        name=_SAGA_NAME,
        version=_SAGA_VERSION,
        body=body,
    )


async def _start_and_kick(trace_id: str) -> SagaStepInstance:
    with patch("engine.logic.assert_prompt_file_exists"):
        await process_saga_event(
            {
                "event_type": "SAGA_STARTED",
                "saga_trace_id": trace_id,
                "namespace": "default",
                "step_span_id": None,
            }
        )
    step = (
        await SagaStepInstance.filter(
            saga_trace_id=trace_id,
            compensates_span_id__isnull=True,
        )
        .order_by("forward_seq")
        .first()
    )
    assert step is not None
    assert step.status == StepStatus.IN_PROGRESS
    return step


async def _complete_step(
    trace_id: str, step: SagaStepInstance, *, data: dict | None = None
) -> None:
    with patch("engine.logic.assert_prompt_file_exists"):
        await process_saga_event(
            {
                "event_type": "STEP_COMPLETED",
                "saga_trace_id": trace_id,
                "namespace": "default",
                "step_span_id": step.span_id,
                "output": {"data": data or {"ok": True}},
            }
        )


@pytest.mark.asyncio
async def test_until_evaluation_failed_persists_loop_exhausted_status() -> None:
    await _seed_worker_and_saga(
        _saga_body(
            max_iterations=2,
            until_cel="true",
            body_steps=[_reason_step("attempt")],
        )
    )
    trace_id = (
        await start_saga(
            namespace="default",
            name=_SAGA_NAME,
            version=_SAGA_VERSION,
            input={},
        )
    ).trace_id
    attempt = await _start_and_kick(trace_id)

    with patch(
        "engine.loop_control.evaluate_until",
        side_effect=PolicyEvaluationError("until boom"),
    ):
        await _complete_step(trace_id, attempt)

    saga = await SagaInstance.get(trace_id=trace_id)
    assert saga.status in (SagaStatus.COMPENSATING, SagaStatus.COMPENSATED)
    assert saga.context["loops"]["refine"]["status"] == LOOP_STATUS_EXHAUSTED
    assert saga.context["loops"]["refine"]["iteration"] == 1
    attempt = await SagaStepInstance.get(span_id=attempt.span_id)
    assert attempt.error_details is not None
    assert attempt.error_details.get("code") == "UNTIL_EVALUATION_FAILED"


@pytest.mark.asyncio
async def test_skipped_loop_body_step_resets_latest_wins_context() -> None:
    saga, steps = await create_saga_with_steps(
        step_count=1,
        initial_context={
            "input": {},
            "steps": {
                "validate": step_context_entry_for_saga({"data": {"ok": True}, "facts": {}}),
            },
            "loops": {"refine": {"iteration": 2, "max_iterations": 2, "status": "running"}},
        },
        status=SagaStatus.RUNNING,
    )
    step = steps[0]
    step.step_id = "validate"
    step.loop_id = "refine"
    step.iteration = 2
    step.status = StepStatus.PENDING
    await step.save()

    await _mark_step_skipped_when_false(saga, step, db_conn=None, trace_context=None)

    saga = await SagaInstance.get(trace_id=saga.trace_id)
    entry = saga.context["steps"]["validate"]
    assert entry == step_context_entry_for_saga(None)
    assert entry["output"]["data"] == {}
    assert "ok" not in entry["output"]["data"]


@pytest.mark.asyncio
async def test_skipped_outside_loop_leaves_context_steps_untouched() -> None:
    prior = step_context_entry_for_saga({"data": {"kept": True}, "facts": {}})
    saga, steps = await create_saga_with_steps(
        step_count=1,
        initial_context={"input": {}, "steps": {"other": prior}, "loops": {}},
        status=SagaStatus.RUNNING,
    )
    step = steps[0]
    step.loop_id = None
    step.status = StepStatus.PENDING
    await step.save()

    await _mark_step_skipped_when_false(saga, step, db_conn=None, trace_context=None)

    saga = await SagaInstance.get(trace_id=saga.trace_id)
    assert saga.context["steps"]["other"] == prior


@pytest.mark.asyncio
async def test_loop_exhausted_when_until_stays_false() -> None:
    await _seed_worker_and_saga(
        _saga_body(
            max_iterations=1,
            until_cel="false",
            body_steps=[_reason_step("attempt")],
        )
    )
    trace_id = (
        await start_saga(
            namespace="default",
            name=_SAGA_NAME,
            version=_SAGA_VERSION,
            input={},
        )
    ).trace_id
    attempt = await _start_and_kick(trace_id)
    await _complete_step(trace_id, attempt)

    saga = await SagaInstance.get(trace_id=trace_id)
    assert saga.status in (SagaStatus.COMPENSATING, SagaStatus.COMPENSATED)
    assert saga.context["loops"]["refine"]["status"] == LOOP_STATUS_EXHAUSTED
    attempt = await SagaStepInstance.get(span_id=attempt.span_id)
    assert attempt.error_details is not None
    assert attempt.error_details.get("code") == "LOOP_EXHAUSTED"
