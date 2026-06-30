"""HITL retry limits and worker argument enrichment."""

import pytest
from common.hitl_retry import (
    HITL_RETRY_ARGS_KEY,
    HitlRetryLimitError,
    assert_hitl_retry_allowed,
    hitl_retries_remaining,
    merge_hitl_retry_into_arguments,
)
from common.models import SagaStatus, StepStatus
from common.schemas.saga import ReasonSagaStep
from engine.hitl_decisions import enqueue_hitl_retry
from tests.factories import create_saga_with_steps


class _StepStub:
    hitl_max_retries: int | None
    hitl_retry_count: int
    hitl_retry_guidance: str | None

    def __init__(
        self,
        *,
        hitl_max_retries: int | None = None,
        hitl_retry_count: int = 0,
        hitl_retry_guidance: str | None = None,
    ) -> None:
        self.hitl_max_retries = hitl_max_retries
        self.hitl_retry_count = hitl_retry_count
        self.hitl_retry_guidance = hitl_retry_guidance


def test_hitl_retries_remaining_unlimited():
    assert hitl_retries_remaining(_StepStub(hitl_max_retries=None)) is None


def test_merge_hitl_retry_into_arguments():
    step = _StepStub(hitl_max_retries=3, hitl_retry_count=2, hitl_retry_guidance="fix the summary")
    merged = merge_hitl_retry_into_arguments({"amount": 1}, step, attempt=2)
    assert merged["amount"] == 1
    assert merged[HITL_RETRY_ARGS_KEY] == {
        "attempt": 2,
        "max_retries": 3,
        "guidance": "fix the summary",
    }


def test_merge_guidance_override_wins():
    step = _StepStub(hitl_retry_count=1, hitl_retry_guidance="manifest default")
    merged = merge_hitl_retry_into_arguments({}, step, guidance_override="one-off note", attempt=1)
    assert merged[HITL_RETRY_ARGS_KEY]["guidance"] == "one-off note"


def test_assert_hitl_retry_allowed_raises_at_limit():
    step = _StepStub(hitl_max_retries=2, hitl_retry_count=2)
    with pytest.raises(HitlRetryLimitError):
        assert_hitl_retry_allowed(step)


def test_saga_yaml_hitl_retry_fields_require_hitl():
    with pytest.raises(ValueError, match="require hitl: true"):
        ReasonSagaStep.model_validate(
            {
                "id": "s",
                "name": "s",
                "kind": "reason",
                "worker": "w",
                "worker_version": "1.0.0",
                "prompt": "p.j2",
                "hitl": False,
                "hitl_max_retries": 1,
            }
        )


@pytest.mark.asyncio
async def test_enqueue_hitl_retry_rejects_at_max():
    saga, steps = await create_saga_with_steps(step_count=1)
    step = steps[0]
    step.status = StepStatus.AWAITING_HUMAN
    step.hitl_max_retries = 1
    step.hitl_retry_count = 1
    await step.save()
    saga.status = SagaStatus.AWAITING_HUMAN
    await saga.save()

    with pytest.raises(HitlRetryLimitError):
        await enqueue_hitl_retry(
            trace_id=saga.trace_id,
            step_span_id=step.span_id,
            namespace=saga.namespace,
        )
