"""HITL manual retry limits and worker argument enrichment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.models import SagaStepInstance

HITL_RETRY_ARGS_KEY = "_hitl_retry"


class HitlRetryLimitError(Exception):
    """Step has exhausted configured manual HITL retries."""

    def __init__(self, *, max_retries: int, retry_count: int) -> None:
        self.max_retries = max_retries
        self.retry_count = retry_count
        super().__init__(f"HITL manual retry limit reached ({retry_count} of {max_retries} used).")


def hitl_retries_remaining(step: SagaStepInstance) -> int | None:
    """Return remaining manual retries, or None when unlimited."""
    if step.hitl_max_retries is None:
        return None
    return max(0, int(step.hitl_max_retries) - int(step.hitl_retry_count))


def assert_hitl_retry_allowed(step: SagaStepInstance) -> None:
    """Raise HitlRetryLimitError when the step cannot accept another manual retry."""
    remaining = hitl_retries_remaining(step)
    if remaining is not None and remaining <= 0:
        raise HitlRetryLimitError(
            max_retries=int(step.hitl_max_retries or 0),
            retry_count=int(step.hitl_retry_count),
        )


def merge_hitl_retry_into_arguments(
    worker_args: dict[str, Any],
    step: SagaStepInstance,
    *,
    guidance_override: str | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Attach ``_hitl_retry`` for the worker/LLM when re-running after a manual HITL retry."""
    attempt_num = attempt if attempt is not None else int(step.hitl_retry_count)
    if attempt_num <= 0 and not guidance_override and not step.hitl_retry_guidance:
        return worker_args
    guidance = (guidance_override or step.hitl_retry_guidance or "").strip()
    merged = dict(worker_args)
    merged[HITL_RETRY_ARGS_KEY] = {
        "attempt": attempt_num,
        "max_retries": step.hitl_max_retries,
        "guidance": guidance,
    }
    return merged
