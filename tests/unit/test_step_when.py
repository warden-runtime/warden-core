"""Per-step ``when.cel`` schedule gate (engine primitive)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from common.models import (
    OutboxEvent,
    SagaInstance,
    SagaStatus,
    StepStatus,
    WorkerDefinition,
)
from common.plugins.registry import register_engine_hooks, reset_registry
from common.policy.cel_eval import PolicyEvaluationError
from common.schemas.saga import StepWhenSpec
from common.step_when import evaluate_step_when, step_when_binding, validate_when_cel_compile
from common.topics import TOPIC_WORKER_COMMANDS
from engine.logic import _schedule_next_forward_step
from engine.registry.service import RegistryService
from pydantic import ValidationError
from tests.factories import create_saga_with_steps
from tortoise.transactions import in_transaction


class _RecordingEngineHooks:
    def __init__(self) -> None:
        self.skipped_summaries: list[dict[str, object]] = []

    async def on_steps_skipped_summary(self, **kwargs: object) -> None:
        self.skipped_summaries.append(dict(kwargs))

    async def on_step_transition(self, **kwargs: object) -> None:
        return None

    async def on_saga_transition(self, **kwargs: object) -> None:
        return None

    async def on_step_scheduled(self, **kwargs: object) -> None:
        return None

    async def on_step_started(self, **kwargs: object) -> None:
        return None

    async def on_saga_created(self, **kwargs: object) -> None:
        return None

    async def on_step_created(self, **kwargs: object) -> None:
        return None

    async def on_manifest_registered(self, **kwargs: object) -> None:
        return None

    async def on_ingest_deduplicated(self, **kwargs: object) -> None:
        return None


@pytest.fixture
def recording_hooks():
    reset_registry()
    hooks = _RecordingEngineHooks()
    register_engine_hooks(hooks)
    yield hooks
    reset_registry()


async def _schedule_after(
    saga: SagaInstance,
    after_order: int,
    *,
    recording_hooks: _RecordingEngineHooks | None = None,
) -> SagaInstance:
    del recording_hooks
    async with in_transaction() as conn:
        locked = (
            await SagaInstance.filter(trace_id=saga.trace_id)
            .using_db(conn)
            .select_for_update()
            .first()
        )
        assert locked is not None
        with patch("engine.logic.assert_prompt_file_exists"):
            await _schedule_next_forward_step(locked, after_order, db_conn=conn)
        return locked


@pytest.mark.asyncio
async def test_register_manifest_rejects_invalid_when_cel():
    await WorkerDefinition.create(
        namespace="default",
        name="when-test-worker",
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="Hi.",
    )
    service = RegistryService()
    bad_saga = """
kind: saga
name: bad-when
namespace: default
version: "1.0.0"
description: Invalid when
steps:
  - id: s1
    kind: reason
    name: S1
    worker: when-test-worker
    worker_version: "1.0.0"
    with: {}
    prompt: noop.j2
    when:
      cel: "!!! not valid cel @@@"
"""
    with pytest.raises(ValueError, match="when.cel is invalid"):
        await service.register_manifest(bad_saga)


@pytest.mark.asyncio
async def test_schedule_skips_step_when_cel_false(recording_hooks):
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={
            "input": {},
            "steps": {"step_0": {"output": {"data": {"action": "noop"}}}},
        },
    )
    step0, step1 = steps[0], steps[1]
    step0.status = StepStatus.COMPLETED
    await step0.save()
    step1.when_cel = "steps.step_0.output.data.action == 'comment'"
    await step1.save()

    await _schedule_after(saga, after_order=0, recording_hooks=recording_hooks)

    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step1.status == StepStatus.SKIPPED
    assert saga.status == SagaStatus.COMPLETED
    assert len(recording_hooks.skipped_summaries) == 1
    assert recording_hooks.skipped_summaries[0]["reason"] == "when_false"
    assert recording_hooks.skipped_summaries[0]["skipped_count"] == 1
    assert not await OutboxEvent.filter(
        saga_trace_id=saga.trace_id,
        destination_topic=TOPIC_WORKER_COMMANDS,
        step_span_id=step1.span_id,
    ).exists()


@pytest.mark.asyncio
async def test_schedule_runs_step_when_cel_true():
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={
            "input": {},
            "steps": {"step_0": {"output": {"data": {"action": "comment"}}}},
        },
    )
    step0, step1 = steps[0], steps[1]
    step0.status = StepStatus.COMPLETED
    await step0.save()
    step1.when_cel = "steps.step_0.output.data.action == 'comment'"
    await step1.save()

    await _schedule_after(saga, after_order=0)

    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step1.status == StepStatus.IN_PROGRESS
    assert saga.status == SagaStatus.RUNNING
    assert await OutboxEvent.filter(saga_trace_id=saga.trace_id).exists()


@pytest.mark.asyncio
async def test_schedule_runs_step_when_omitted():
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={"input": {}, "steps": {}},
    )
    step0, step1 = steps[0], steps[1]
    step0.status = StepStatus.COMPLETED
    await step0.save()
    assert step1.when_cel is None

    await _schedule_after(saga, after_order=0)

    await step1.refresh_from_db()
    assert step1.status == StepStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_schedule_fails_step_on_when_eval_error():
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={
            "input": {},
            "steps": {"step_0": {"output": {"data": {"count": 1}}}},
        },
    )
    step0, step1 = steps[0], steps[1]
    step0.status = StepStatus.COMPLETED
    await step0.save()
    step1.when_cel = "steps.step_0.output.data.count / 0 == 1"
    await step1.save()

    await _schedule_after(saga, after_order=0)

    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step1.status == StepStatus.FAILED
    assert step1.error_details.get("code") == "WHEN_EVALUATION_FAILED"
    assert saga.status == SagaStatus.COMPENSATED


@pytest.mark.asyncio
async def test_schedule_skips_first_step_when_cel_false():
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={"input": {"run": False}, "steps": {}},
    )
    step0, step1 = steps[0], steps[1]
    step0.when_cel = "input.run == true"
    await step0.save()

    await _schedule_after(saga, after_order=-1)

    await step0.refresh_from_db()
    await step1.refresh_from_db()
    assert step0.status == StepStatus.SKIPPED
    assert step1.status == StepStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_schedule_completes_when_all_remaining_steps_skipped(recording_hooks):
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={"input": {"run": False}, "steps": {}},
    )
    step0, step1 = steps[0], steps[1]
    step0.when_cel = "input.run == true"
    step1.when_cel = "input.run == true"
    await step0.save()
    await step1.save()

    await _schedule_after(saga, after_order=-1, recording_hooks=recording_hooks)

    await saga.refresh_from_db()
    await step0.refresh_from_db()
    await step1.refresh_from_db()
    assert step0.status == StepStatus.SKIPPED
    assert step1.status == StepStatus.SKIPPED
    assert saga.status == SagaStatus.COMPLETED
    assert recording_hooks.skipped_summaries[0]["skipped_count"] == 2


def test_validate_when_cel_compile_accepts_valid_expression():
    validate_when_cel_compile("input.run == true")


def test_validate_when_cel_compile_rejects_invalid_expression():
    with pytest.raises(PolicyEvaluationError):
        validate_when_cel_compile("@@@ invalid @@@")


@pytest.mark.asyncio
async def test_step_when_binding_and_evaluate():
    saga, steps = await create_saga_with_steps(
        step_count=1,
        initial_context={
            "input": {"flag": True},
            "steps": {"step_0": {"output": {"data": {"ok": 1}}}},
        },
    )
    step = steps[0]
    binding = step_when_binding(saga=saga, step=step)
    assert binding["input"]["flag"] is True
    assert evaluate_step_when(cel_source="input.flag == true", binding=binding) is True
    assert (
        evaluate_step_when(cel_source="steps.step_0.output.data.ok == 1", binding=binding) is True
    )


def test_step_when_spec_validation_errors():
    with pytest.raises(ValidationError):
        StepWhenSpec.model_validate({"cel": ""})
