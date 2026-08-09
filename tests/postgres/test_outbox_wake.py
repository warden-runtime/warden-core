"""Postgres outbox atomic claim + LISTEN/NOTIFY wake tests."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from common.messaging.postgres import PostgresQueueConsumer
from common.models import OutboxEvent, OutboxStatus
from tortoise import Tortoise

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.postgres

_WAKE_TOPIC = "test-outbox-wake"
_CLAIM_TOPIC = "test-outbox-atomic-claim"


async def _apply_notify_trigger(migrations_dir: Path) -> None:
    conn = Tortoise.get_connection("default")
    sql = await asyncio.to_thread(
        (migrations_dir / "005_outbox_notify_trigger.sql").read_text,
        encoding="utf-8",
    )
    await conn.execute_script(sql)


async def _seed_pending(*, topic: str, payload: dict | None = None) -> OutboxEvent:
    return await OutboxEvent.create(
        namespace="default",
        saga_trace_id="a" * 32,
        step_span_id="b" * 16,
        event_type="DO_STEP",
        destination_topic=topic,
        payload=payload or {"ok": True},
        status=OutboxStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_atomic_claim_marks_in_progress_before_handler() -> None:
    """Claim txn flips PENDING→IN_PROGRESS before handler tasks run."""
    seen_status: list[str] = []

    async def handler(_payload: dict) -> None:
        rows = await OutboxEvent.filter(destination_topic=_CLAIM_TOPIC).all()
        assert len(rows) == 1
        seen_status.append(rows[0].status.value)

    consumer = PostgresQueueConsumer(
        topic=_CLAIM_TOPIC,
        group_id="claim-test",
        handler=handler,
        poll_interval=0.05,
        wake_enabled=False,
    )
    await _seed_pending(topic=_CLAIM_TOPIC)
    claimed = await consumer._poll_and_dispatch()
    assert claimed == 1
    row = await OutboxEvent.get(destination_topic=_CLAIM_TOPIC)
    assert row.status == OutboxStatus.IN_PROGRESS
    await consumer._drain_in_flight()
    assert seen_status == [OutboxStatus.IN_PROGRESS.value]
    await row.refresh_from_db()
    assert row.status == OutboxStatus.COMPLETED


@pytest.mark.asyncio
async def test_drain_reclaim_does_not_double_dispatch() -> None:
    """Immediate re-poll after claim must not run the same row twice."""
    runs: list[int] = []

    async def handler(_payload: dict) -> None:
        runs.append(1)
        await asyncio.sleep(0.05)

    consumer = PostgresQueueConsumer(
        topic=_CLAIM_TOPIC + "-drain",
        group_id="drain-test",
        handler=handler,
        poll_interval=1.0,
        wake_enabled=False,
        batch_size=10,
    )
    topic = consumer.topic
    await _seed_pending(topic=topic)
    n1 = await consumer._poll_and_dispatch()
    n2 = await consumer._poll_and_dispatch()
    assert n1 == 1
    assert n2 == 0
    await consumer._drain_in_flight()
    assert runs == [1]


@pytest.mark.asyncio
async def test_notify_wake_beats_poll_interval(
    migrations_dir: Path,
    postgres_url: str,
) -> None:
    """With wake on and poll_interval=1s, insert PENDING is claimed well under 1s."""
    await _apply_notify_trigger(migrations_dir)
    from common.config import get_settings

    get_settings.cache_clear()
    get_settings().db_url = postgres_url

    done = asyncio.Event()

    async def handler(_payload: dict) -> None:
        done.set()

    consumer = PostgresQueueConsumer(
        topic=_WAKE_TOPIC,
        group_id="wake-test",
        handler=handler,
        poll_interval=1.0,
        wake_enabled=True,
    )
    task = asyncio.create_task(consumer.start())
    try:
        await asyncio.sleep(0.15)  # allow LISTEN to attach
        t0 = time.perf_counter()
        await _seed_pending(topic=_WAKE_TOPIC)
        await asyncio.wait_for(done.wait(), timeout=0.8)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.8, f"wake took {elapsed:.3f}s (expected ≪ 1s poll)"
    finally:
        await consumer.stop()
        await asyncio.wait_for(task, timeout=2.0)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_update_to_pending_also_wakes(
    migrations_dir: Path,
    postgres_url: str,
) -> None:
    """Requeue (UPDATE → PENDING) must NOTIFY the same as insert."""
    await _apply_notify_trigger(migrations_dir)
    from common.config import get_settings

    get_settings.cache_clear()
    get_settings().db_url = postgres_url

    topic = _WAKE_TOPIC + "-requeue"
    done = asyncio.Event()

    async def handler(_payload: dict) -> None:
        done.set()

    consumer = PostgresQueueConsumer(
        topic=topic,
        group_id="wake-requeue-test",
        handler=handler,
        poll_interval=1.0,
        wake_enabled=True,
    )
    row = await OutboxEvent.create(
        namespace="default",
        saga_trace_id="c" * 32,
        step_span_id="d" * 16,
        event_type="DO_STEP",
        destination_topic=topic,
        payload={"requeue": True},
        status=OutboxStatus.IN_PROGRESS,
    )
    task = asyncio.create_task(consumer.start())
    try:
        await asyncio.sleep(0.15)
        t0 = time.perf_counter()
        await OutboxEvent.filter(id=row.id).update(status=OutboxStatus.PENDING)
        await asyncio.wait_for(done.wait(), timeout=0.8)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.8, f"requeue wake took {elapsed:.3f}s"
    finally:
        await consumer.stop()
        await asyncio.wait_for(task, timeout=2.0)
        get_settings.cache_clear()
