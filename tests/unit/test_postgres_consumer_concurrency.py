"""Unit tests for bounded in-flight dispatch on PostgresQueueConsumer."""

from __future__ import annotations

import asyncio

import pytest
from common.messaging.postgres import PostgresQueueConsumer
from common.models import OutboxEvent, OutboxStatus

_TEST_TOPIC = "test-worker-concurrency"


async def _seed_pending_rows(count: int) -> None:
    for index in range(count):
        await OutboxEvent.create(
            namespace="default",
            saga_trace_id="a" * 32,
            step_span_id="b" * 16,
            event_type="DO_STEP",
            destination_topic=_TEST_TOPIC,
            payload={"index": index},
            status=OutboxStatus.PENDING,
        )


async def _dispatch_pending_rows(consumer: PostgresQueueConsumer) -> None:
    """Spawn handler tasks for pending rows (ORM fetch; avoids Postgres-only SKIP LOCKED)."""
    events = (
        await OutboxEvent.filter(
            destination_topic=consumer.topic,
            status=OutboxStatus.PENDING,
        )
        .order_by("created_at")
        .limit(consumer._batch_size)
    )
    for event in events:
        if consumer._shutdown.is_set():
            break
        row = {
            "id": event.id,
            "payload": event.payload,
            "trace_context": event.trace_context,
            "namespace": event.namespace,
            "saga_trace_id": event.saga_trace_id,
            "step_span_id": event.step_span_id,
            "event_type": event.event_type,
        }
        task = asyncio.create_task(consumer._process_row(row))
        consumer._track_task(task)


def _make_tracking_handler(
    *,
    delay: float,
    completions: list[int],
    peak_in_flight: list[int],
) -> object:
    state = {"in_flight": 0}

    async def handler(_payload: dict) -> None:
        state["in_flight"] += 1
        peak_in_flight[0] = max(peak_in_flight[0], state["in_flight"])
        await asyncio.sleep(delay)
        state["in_flight"] -= 1
        completions.append(1)

    return handler


@pytest.mark.asyncio
async def test_max_in_flight_caps_concurrent_handlers() -> None:
    completions: list[int] = []
    peak: list[int] = [0]
    consumer = PostgresQueueConsumer(
        topic=_TEST_TOPIC,
        group_id="test-group",
        handler=_make_tracking_handler(
            delay=0.08,
            completions=completions,
            peak_in_flight=peak,
        ),
        max_in_flight=2,
    )
    await _seed_pending_rows(6)
    await _dispatch_pending_rows(consumer)
    await consumer._drain_in_flight()

    assert peak[0] <= 2
    assert len(completions) == 6
    completed_rows = await OutboxEvent.filter(
        destination_topic=_TEST_TOPIC,
        status=OutboxStatus.COMPLETED,
    ).count()
    assert completed_rows == 6


@pytest.mark.asyncio
async def test_default_max_in_flight_is_serial() -> None:
    completions: list[int] = []
    peak: list[int] = [0]
    consumer = PostgresQueueConsumer(
        topic=_TEST_TOPIC,
        group_id="test-group",
        handler=_make_tracking_handler(
            delay=0.03,
            completions=completions,
            peak_in_flight=peak,
        ),
    )
    await _seed_pending_rows(4)
    await _dispatch_pending_rows(consumer)
    await consumer._drain_in_flight()

    assert peak[0] == 1
    assert len(completions) == 4


@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_handlers() -> None:
    completions: list[int] = []
    consumer = PostgresQueueConsumer(
        topic=_TEST_TOPIC,
        group_id="test-group",
        handler=_make_tracking_handler(
            delay=0.06,
            completions=completions,
            peak_in_flight=[0],
        ),
        max_in_flight=3,
    )
    await _seed_pending_rows(3)
    await _dispatch_pending_rows(consumer)
    assert len(completions) == 0
    await consumer._drain_in_flight()
    assert len(completions) == 3


@pytest.mark.asyncio
async def test_start_drains_in_flight_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    completions: list[int] = []
    consumer = PostgresQueueConsumer(
        topic=_TEST_TOPIC,
        group_id="test-group",
        handler=_make_tracking_handler(
            delay=0.08,
            completions=completions,
            peak_in_flight=[0],
        ),
        poll_interval=0.02,
        max_in_flight=2,
    )
    await _seed_pending_rows(2)

    async def _poll_once_then_stop(self: PostgresQueueConsumer) -> None:
        await _dispatch_pending_rows(self)
        self._shutdown.set()

    monkeypatch.setattr(PostgresQueueConsumer, "_poll_and_dispatch", _poll_once_then_stop)
    await consumer.start()
    assert len(completions) == 2


@pytest.mark.asyncio
async def test_max_in_flight_rejects_zero() -> None:
    with pytest.raises(ValueError, match="max_in_flight"):
        PostgresQueueConsumer(
            topic=_TEST_TOPIC,
            group_id="g",
            handler=_make_tracking_handler(
                delay=0,
                completions=[],
                peak_in_flight=[0],
            ),
            max_in_flight=0,
        )
