"""Engine smoke: max_iterations:1 + until.cel true exits and materializes the tail."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from common.models import (
    SagaDefinition,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepStatus,
    WorkerDefinition,
)
from engine.api.saga_start import start_saga
from engine.logic import process_saga_event

_WORKER = "loop-smoke-worker"
_SAGA_NAME = "loop-smoke"
_SAGA_VERSION = "1.0.0"

_LOOP_SMOKE_BODY = {
    "kind": "saga",
    "name": _SAGA_NAME,
    "namespace": "default",
    "version": _SAGA_VERSION,
    "description": "Loop smoke: one body pass then exit",
    "steps": [
        {
            "id": "refine",
            "kind": "loop",
            "max_iterations": 1,
            "until": {"cel": "true"},
            "steps": [
                {
                    "id": "attempt",
                    "kind": "reason",
                    "name": "Attempt",
                    "worker": _WORKER,
                    "worker_version": "1.0.0",
                    "agent-adapter": "simple",
                    "with": {},
                    "prompt": "noop.j2",
                    "tools": {"allow": []},
                    "timeout_seconds": 600,
                }
            ],
        },
        {
            "id": "finalize",
            "kind": "reason",
            "name": "Finalize",
            "worker": _WORKER,
            "worker_version": "1.0.0",
            "agent-adapter": "simple",
            "with": {},
            "prompt": "noop.j2",
            "tools": {"allow": []},
            "timeout_seconds": 600,
        },
    ],
}


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
async def test_loop_smoke_until_true_exits_and_runs_tail() -> None:
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
        body=_LOOP_SMOKE_BODY,
    )

    trace_id = (
        await start_saga(
            namespace="default",
            name=_SAGA_NAME,
            version=_SAGA_VERSION,
            input={},
        )
    ).trace_id
    steps = await SagaStepInstance.filter(
        saga_trace_id=trace_id,
        compensates_span_id__isnull=True,
    ).order_by("forward_seq")
    assert [s.step_id for s in steps] == ["attempt"]
    assert steps[0].loop_id == "refine"
    assert steps[0].iteration == 1
    assert steps[0].forward_seq == 0

    with patch("engine.logic.assert_prompt_file_exists"):
        await process_saga_event(
            {
                "event_type": "SAGA_STARTED",
                "saga_trace_id": trace_id,
                "namespace": "default",
                "step_span_id": None,
            }
        )

    attempt = await SagaStepInstance.get(span_id=steps[0].span_id)
    assert attempt.status == StepStatus.IN_PROGRESS

    await _complete_step(trace_id, attempt)

    steps_after = await SagaStepInstance.filter(
        saga_trace_id=trace_id,
        compensates_span_id__isnull=True,
    ).order_by("forward_seq")
    assert [s.step_id for s in steps_after] == ["attempt", "finalize"]
    finalize = steps_after[1]
    assert finalize.loop_id is None
    assert finalize.iteration is None
    assert finalize.forward_seq == 1
    assert finalize.status == StepStatus.IN_PROGRESS

    saga = await SagaInstance.get(trace_id=trace_id)
    assert saga.context["loops"]["refine"]["status"] == "exited"
    assert saga.context["loops"]["refine"]["iteration"] == 1

    await _complete_step(trace_id, finalize, data={"done": True})

    saga = await SagaInstance.get(trace_id=trace_id)
    assert saga.status == SagaStatus.COMPLETED
    finalize = await SagaStepInstance.get(span_id=finalize.span_id)
    assert finalize.status == StepStatus.COMPLETED
