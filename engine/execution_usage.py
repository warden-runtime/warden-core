"""Engine-side step execution usage merge (worker tokens only)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.execution_usage import merge_execution_usage, worker_usage_from_event

if TYPE_CHECKING:
    from common.models import SagaStepInstance
    from tortoise.backends.base.client import BaseDBAsyncClient


async def finalize_step_execution_usage(
    step: SagaStepInstance,
    *,
    worker_usage: dict[str, Any] | None,
    conn: BaseDBAsyncClient,
) -> dict[str, Any] | None:
    """Persist worker usage onto the step row (sibling of timing finalize)."""
    worker = worker_usage_from_event(worker_usage)
    existing = step.execution_usage if isinstance(step.execution_usage, dict) else None
    merged = merge_execution_usage(worker=worker, existing=existing)
    step.execution_usage = merged or None
    return step.execution_usage


def clear_step_usage_fields(step: SagaStepInstance) -> None:
    step.execution_usage = None


async def merge_step_usage_if_needed(
    step: SagaStepInstance,
    *,
    worker_usage: dict[str, Any] | None,
    conn: BaseDBAsyncClient,
) -> None:
    """Write usage when the column is unset or worker payload is present."""
    if worker_usage or not step.execution_usage:
        await finalize_step_execution_usage(step, worker_usage=worker_usage, conn=conn)
