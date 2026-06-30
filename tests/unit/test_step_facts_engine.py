"""Engine merge of tool-facts into saga context and when.cel routing."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from common.models import OutboxEvent, SagaStatus, StepStatus
from common.topics import TOPIC_WORKER_COMMANDS
from engine.logic import _schedule_next_forward_step, process_saga_event
from tests.factories import create_saga_with_steps
from tortoise.transactions import in_transaction

_FACTS_WHEN_CEL = (
    "has(steps.triage.facts.triage_metrics) && steps.triage.facts.triage_metrics.total_count > 0"
)


async def _ingest_step_completed(
    saga,
    step,
    *,
    data: dict,
    facts: dict | None = None,
) -> None:
    output: dict = {"data": data}
    if facts is not None:
        output["facts"] = facts
    await process_saga_event(
        {
            "event_type": "STEP_COMPLETED",
            "saga_trace_id": saga.trace_id,
            "namespace": saga.namespace,
            "step_span_id": step.span_id,
            "output": output,
        }
    )


async def _schedule_after(saga, after_order: int) -> None:
    async with in_transaction() as conn:
        locked = (
            await type(saga)
            .filter(trace_id=saga.trace_id)
            .using_db(conn)
            .select_for_update()
            .first()
        )
        assert locked is not None
        with patch("engine.logic.assert_prompt_file_exists"):
            await _schedule_next_forward_step(locked, after_order, db_conn=conn)


@pytest.mark.asyncio
async def test_step_completed_merges_output_and_facts_into_context() -> None:
    saga, steps = await create_saga_with_steps(
        step_count=1,
        status=SagaStatus.RUNNING,
        initial_context={"input": {}, "steps": {"step_0": {"output": {"data": {}}, "facts": {}}}},
    )
    step = steps[0]
    step.status = StepStatus.IN_PROGRESS
    await step.save()

    await _ingest_step_completed(
        saga,
        step,
        data={"summary": "done"},
        facts={"list_issues": {"total_count": 2}},
    )

    await saga.refresh_from_db()
    entry = saga.context["steps"]["step_0"]
    assert entry["output"] == {"data": {"summary": "done"}}
    assert entry["facts"] == {"list_issues": {"total_count": 2}}


@pytest.mark.asyncio
async def test_when_skips_commit_when_facts_absent() -> None:
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={
            "input": {},
            "steps": {
                "step_0": {
                    "output": {"data": {"summary": "no issues"}},
                    "facts": {},
                },
                "step_1": {"output": {"data": {}}, "facts": {}},
            },
        },
    )
    step0, step1 = steps[0], steps[1]
    step0.step_id = "triage"
    step1.step_id = "post-comment"
    step0.status = StepStatus.COMPLETED
    step1.when_cel = _FACTS_WHEN_CEL
    await step0.save()
    await step1.save()

    await _schedule_after(saga, after_order=0)

    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step1.status == StepStatus.SKIPPED
    assert saga.status == SagaStatus.COMPLETED


@pytest.mark.asyncio
async def test_when_skips_commit_when_total_count_zero() -> None:
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={
            "input": {},
            "steps": {
                "step_0": {
                    "output": {"data": {"summary": "empty"}},
                    "facts": {"triage_metrics": {"total_count": 0}},
                },
                "step_1": {"output": {"data": {}}, "facts": {}},
            },
        },
    )
    step0, step1 = steps[0], steps[1]
    step0.step_id = "triage"
    step1.step_id = "post-comment"
    step0.status = StepStatus.COMPLETED
    step1.when_cel = _FACTS_WHEN_CEL
    await step0.save()
    await step1.save()

    await _schedule_after(saga, after_order=0)

    await step1.refresh_from_db()
    assert step1.status == StepStatus.SKIPPED


@pytest.mark.asyncio
async def test_when_runs_commit_when_total_count_positive() -> None:
    saga, steps = await create_saga_with_steps(
        step_count=2,
        status=SagaStatus.RUNNING,
        initial_context={
            "input": {},
            "steps": {
                "triage": {
                    "output": {
                        "data": {
                            "summary": "issue found",
                            "recommended_issue_number": 1,
                            "comment_body": "## Warden triage\n\nx",
                        }
                    },
                    "facts": {"triage_metrics": {"total_count": 2}},
                },
                "post-comment": {"output": {"data": {}}, "facts": {}},
            },
        },
    )
    step0, step1 = steps[0], steps[1]
    step0.step_id = "triage"
    step1.step_id = "post-comment"
    step0.status = StepStatus.COMPLETED
    step1.when_cel = _FACTS_WHEN_CEL
    await step0.save()
    await step1.save()

    await _schedule_after(saga, after_order=0)

    await step1.refresh_from_db()
    await saga.refresh_from_db()
    assert step1.status == StepStatus.IN_PROGRESS
    assert saga.status == SagaStatus.RUNNING
    assert await OutboxEvent.filter(
        saga_trace_id=saga.trace_id,
        destination_topic=TOPIC_WORKER_COMMANDS,
        step_span_id=step1.span_id,
    ).exists()
