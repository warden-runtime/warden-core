"""
Postgres adapter: write to OutboxEvent (status=PENDING), poll with SKIP LOCKED.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from tortoise import connections
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError

from common.messaging.protocols import MessageQueueConsumer, MessageQueueProducer
from common.models import OutboxEvent, OutboxStatus
from common.outbox_timestamps import utc_now

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 1.0  # seconds
DEFAULT_BATCH_SIZE = 10

_ENVELOPE_FIELDS = ("event_type", "saga_trace_id", "namespace", "step_span_id")


def _parse_json_value(value: Any, *, on_decode_error: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return on_decode_error


def _assemble_consumer_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Build handler payload from an outbox row (sync; may parse JSON strings)."""
    payload = _parse_json_value(row.get("payload") or {}, on_decode_error={})
    if not isinstance(payload, dict):
        payload = {"raw": payload}

    for field in _ENVELOPE_FIELDS:
        if row.get(field):
            payload = {**payload, field: row[field]}

    if row.get("trace_context") is not None:
        trace_context = _parse_json_value(row["trace_context"], on_decode_error={})
        if not isinstance(trace_context, dict):
            trace_context = {}
        payload = {**payload, "trace_context": trace_context}

    return payload


class PostgresQueueProducer(MessageQueueProducer):
    """Writes exactly one row to OutboxEvent with status=PENDING."""

    async def publish(
        self,
        topic: str,
        payload: dict,
        *,
        headers: dict | None = None,
        conn: BaseDBAsyncClient | None = None,
    ) -> None:
        """Write one row to OutboxEvent with status=PENDING.

        Uses headers for namespace, saga_trace_id, step_span_id, event_type,
        trace_context, optional idempotency_key. When idempotency_key is present,
        at most one row per (namespace, destination_topic, idempotency_key) is created;
        duplicate writes are no-op. When conn is provided, write is in that transaction.
        """
        headers = headers or {}
        # Routing fields: required for outbox envelope and consumers
        namespace = headers.get("namespace", "default")
        saga_trace_id = headers.get("saga_trace_id", "")
        step_span_id = headers.get("step_span_id", "")
        event_type = headers.get("event_type", "")
        idempotency_key = headers.get("idempotency_key")
        trace_context = headers.get("trace_context")
        if trace_context is None:
            trace_context = {}
        if isinstance(trace_context, str):
            trace_context = _parse_json_value(trace_context, on_decode_error={"raw": trace_context})
        if not isinstance(trace_context, dict):
            trace_context = {}

        try:
            await OutboxEvent.create(
                namespace=namespace,
                saga_trace_id=saga_trace_id,
                step_span_id=step_span_id,
                event_type=event_type,
                destination_topic=topic,
                idempotency_key=idempotency_key,
                trace_context=trace_context,
                payload=payload,
                status=OutboxStatus.PENDING,
                using_db=conn,
            )
            logger.debug("Outbox event queued for topic %s trace %s", topic, saga_trace_id)
        except IntegrityError:
            if idempotency_key is not None:
                logger.debug(
                    "Duplicate write detected for topic=%s idempotency_key=%s; "
                    "skipping outbox insertion",
                    topic,
                    idempotency_key,
                )
            else:
                raise


class PostgresQueueConsumer(MessageQueueConsumer):
    """Polls outbox with SKIP LOCKED, marks IN_PROGRESS, invokes handler, then COMPLETED/FAILED."""

    def __init__(
        self,
        topic: str,
        group_id: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_in_flight: int = 1,
    ) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be >= 1")
        super().__init__(topic=topic, group_id=group_id, handler=handler)
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._max_in_flight = max_in_flight
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._in_flight: set[asyncio.Task[None]] = set()
        self._shutdown = asyncio.Event()

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._in_flight.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._in_flight.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.exception("Outbox handler task failed: %s", exc)

        task.add_done_callback(_done)

    async def start(self) -> None:
        self._shutdown.clear()
        logger.info(
            "Postgres consumer started topic=%s group_id=%s max_in_flight=%d",
            self.topic,
            self.group_id,
            self._max_in_flight,
        )
        try:
            while not self._shutdown.is_set():
                try:
                    await self._poll_and_dispatch()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception("Consumer loop error: %s", e)
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass
        finally:
            await self._drain_in_flight()
        logger.info("Postgres consumer stopped topic=%s", self.topic)

    async def stop(self) -> None:
        self._shutdown.set()

    async def _drain_in_flight(self) -> None:
        if not self._in_flight:
            return
        results = await asyncio.gather(*self._in_flight, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.exception("In-flight handler error during drain: %s", result)

    async def _set_outbox_status(
        self,
        outbox_id: Any,
        *,
        status: OutboxStatus,
        require_current: OutboxStatus | None = None,
    ) -> int:
        now = utc_now()
        q = OutboxEvent.filter(id=outbox_id)
        if require_current is not None:
            q = q.filter(status=require_current)
        return await q.update(status=status, updated_at=now)

    async def _process_row(self, row: dict[str, Any]) -> None:
        async with self._semaphore:
            outbox_id = row["id"]
            payload = await asyncio.to_thread(_assemble_consumer_payload, row)

            await self._set_outbox_status(outbox_id, status=OutboxStatus.IN_PROGRESS)

            try:
                await self.handler(payload)
                updated = await self._set_outbox_status(
                    outbox_id,
                    status=OutboxStatus.COMPLETED,
                    require_current=OutboxStatus.IN_PROGRESS,
                )
                if not updated:
                    logger.info(
                        "outbox_completion_skipped_reaped id=%s reason=status_no_longer_in_progress",
                        outbox_id,
                    )
            except Exception as e:
                logger.exception("Handler failed for outbox id %s: %s", outbox_id, e)
                updated = await self._set_outbox_status(
                    outbox_id,
                    status=OutboxStatus.FAILED,
                    require_current=OutboxStatus.IN_PROGRESS,
                )
                if not updated:
                    logger.info(
                        "outbox_completion_skipped_reaped id=%s reason=status_no_longer_in_progress",
                        outbox_id,
                    )

    async def _poll_and_dispatch(self) -> None:
        connection = connections.get("default")
        sql = """
            SELECT id, payload, trace_context, namespace, saga_trace_id, step_span_id, event_type
            FROM outbox_events
            WHERE destination_topic = $1 AND status = $2
            ORDER BY created_at
            LIMIT $3
            FOR UPDATE SKIP LOCKED
        """
        rows = await connection.execute_query_dict(
            sql,
            [self.topic, OutboxStatus.PENDING.value, self._batch_size],
        )
        if not rows:
            return

        for row in rows:
            if self._shutdown.is_set():
                break
            task = asyncio.create_task(self._process_row(row))
            self._track_task(task)
