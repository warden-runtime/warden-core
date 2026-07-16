"""Engine-side step execution timing merge and outbox lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.execution_timing import (
    EngineTimingAccumulator,
    engine_timing_from_pending,
    merge_execution_timing,
    merge_pending_engine,
    worker_timing_from_event,
)
from common.models import OutboxEvent, SagaStepInstance
from common.topics import TOPIC_WORKER_COMMANDS

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient


async def fetch_worker_command_outbox_created_at(
    *,
    namespace: str,
    idempotency_key: str,
    conn: BaseDBAsyncClient,
) -> Any:
    row = (
        await OutboxEvent.filter(
            namespace=namespace,
            destination_topic=TOPIC_WORKER_COMMANDS,
            idempotency_key=idempotency_key,
        )
        .using_db(conn)
        .first()
    )
    return row.created_at if row is not None else None


async def finalize_step_execution_timing(
    step: SagaStepInstance,
    *,
    worker_timing: dict[str, Any] | None,
    conn: BaseDBAsyncClient,
    ingest_acc: EngineTimingAccumulator | None = None,
) -> dict[str, Any]:
    """Merge worker + staged + ingest engine buckets onto the step row."""
    pending = step.pending_engine_timing if isinstance(step.pending_engine_timing, dict) else {}
    ingest = ingest_acc or EngineTimingAccumulator()
    ingest.record_dispatch_to_ingest(
        pending=pending,
        outbox_created_at=await fetch_worker_command_outbox_created_at(
            namespace=step.namespace,
            idempotency_key=step.idempotency_key,
            conn=conn,
        ),
    )
    engine_buckets = engine_timing_from_pending(pending)
    for key, val in ingest.to_dict().items():
        engine_buckets[key] = engine_buckets.get(key, 0) + val
    merged = merge_execution_timing(
        worker=worker_timing_from_event(worker_timing),
        engine=engine_buckets or None,
        existing=step.execution_timing if isinstance(step.execution_timing, dict) else None,
    )
    step.execution_timing = merged or None
    step.pending_engine_timing = None
    dispatch_ms = ingest.dispatch_to_ingest_ms
    if dispatch_ms > 0:
        from common.telemetry import record_timing_bucket_on_current_span

        record_timing_bucket_on_current_span(
            section="engine",
            bucket="dispatch_to_ingest_ms",
            ms=dispatch_ms,
        )
    return merged


def clear_step_timing_fields(step: SagaStepInstance) -> None:
    """Clear timing (and sibling usage) staging before retry / recovery redispatch."""
    step.execution_timing = None
    step.pending_engine_timing = None
    step.execution_usage = None


def add_engine_bucket_ms(
    step: SagaStepInstance,
    *,
    bucket: str,
    ms: int,
) -> None:
    from common.execution_timing import ENGINE_BUCKETS, clamp_nonneg, merge_execution_timing

    if bucket not in ENGINE_BUCKETS:
        return
    existing = step.execution_timing if isinstance(step.execution_timing, dict) else None
    engine = dict((existing or {}).get("engine") or {})
    engine[bucket] = engine.get(bucket, 0) + clamp_nonneg(ms)
    step.execution_timing = merge_execution_timing(engine=engine, existing=existing)
    cumulative = engine.get(bucket, 0)
    if cumulative > 0:
        from common.telemetry import record_timing_bucket_on_current_span

        record_timing_bucket_on_current_span(section="engine", bucket=bucket, ms=cumulative)


async def merge_step_timing_if_needed(
    step: SagaStepInstance,
    *,
    worker_timing: dict[str, Any] | None,
    conn: BaseDBAsyncClient,
) -> None:
    """Merge timing when pending staging exists or execution_timing is unset."""
    pending = step.pending_engine_timing
    has_pending = isinstance(pending, dict) and bool(pending)
    if has_pending or not step.execution_timing:
        await finalize_step_execution_timing(step, worker_timing=worker_timing, conn=conn)


async def persist_schedule_engine_timing_on_policy_denial(
    step: SagaStepInstance,
    schedule_acc: EngineTimingAccumulator,
    *,
    conn: BaseDBAsyncClient,
) -> None:
    schedule_acc.stop("schedule", bucket="schedule_ms")
    if not schedule_acc.to_dict():
        return
    step.execution_timing = merge_execution_timing(engine=schedule_acc.to_dict())
    await step.save(using_db=conn, update_fields=["execution_timing"])


async def persist_pending_engine_timing(
    step: SagaStepInstance,
    *,
    engine_add: dict[str, int] | None = None,
    dispatch_anchor: float | None = None,
    conn: BaseDBAsyncClient,
) -> None:
    pending = step.pending_engine_timing if isinstance(step.pending_engine_timing, dict) else None
    step.pending_engine_timing = merge_pending_engine(
        pending,
        engine_add=engine_add,
        dispatch_anchor=dispatch_anchor,
    )
    await step.save(using_db=conn, update_fields=["pending_engine_timing"])
