"""
Postgres adapter: write to OutboxEvent (status=PENDING), claim with SKIP LOCKED,
optional LISTEN/NOTIFY idle wake.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import asyncpg
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from common.config import get_settings
from common.messaging.notify import topic_to_notify_channel
from common.messaging.protocols import MessageQueueConsumer, MessageQueueProducer
from common.models import OutboxEvent, OutboxStatus
from common.outbox_timestamps import utc_now

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tortoise.backends.base.client import BaseDBAsyncClient

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


def _asyncpg_dsn(db_url: str) -> str:
    """Normalize Tortoise-style postgres URLs for asyncpg.connect."""
    parsed = urlparse(db_url)
    scheme = parsed.scheme.split("+", 1)[0].lower()
    if scheme in ("postgres", "postgresql"):
        return urlunparse(parsed._replace(scheme="postgresql"))
    return db_url


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
    """Claim outbox with SKIP LOCKED, invoke handler, then COMPLETED/FAILED.

    Idle wait uses a fixed poll interval, or (when wake_enabled) LISTEN/NOTIFY on a
    dedicated asyncpg connection with the poll interval as a safety timeout.
    """

    def __init__(
        self,
        topic: str,
        group_id: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_in_flight: int = 1,
        wake_enabled: bool = False,
    ) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be >= 1")
        super().__init__(topic=topic, group_id=group_id, handler=handler)
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._max_in_flight = max_in_flight
        self._wake_enabled = wake_enabled
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._in_flight: set[asyncio.Task[None]] = set()
        self._shutdown = asyncio.Event()
        self._notify_event = asyncio.Event()
        self._listen_conn: asyncpg.Connection | None = None
        self._listen_channel: str | None = None
        self._listen_callback: Any | None = None

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
            "Postgres consumer started topic=%s group_id=%s max_in_flight=%d wake_enabled=%s",
            self.topic,
            self.group_id,
            self._max_in_flight,
            self._wake_enabled,
        )
        try:
            if self._wake_enabled:
                await self._ensure_listen()
            while not self._shutdown.is_set():
                try:
                    claimed = await self._poll_and_dispatch()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("Consumer loop error: %s", e)
                    claimed = 0
                if claimed > 0:
                    continue
                try:
                    await self._idle_wait()
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            self._shutdown.set()
            raise
        finally:
            await self._close_listen()
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

    async def _ensure_listen(self) -> None:
        if not self._wake_enabled:
            return
        if self._listen_conn is not None and not self._listen_conn.is_closed():
            return
        try:
            await self._open_listen()
        except Exception:
            logger.warning(
                "Outbox LISTEN setup failed topic=%s; falling back to poll-only idle wait",
                self.topic,
                exc_info=True,
            )
            await self._close_listen()

    async def _open_listen(self) -> None:
        await self._close_listen()
        dsn = _asyncpg_dsn(get_settings().db_url)
        channel = topic_to_notify_channel(self.topic)
        conn = await asyncpg.connect(dsn)
        notify_event = self._notify_event

        def _on_notify(
            _connection: asyncpg.Connection,
            _pid: int,
            _chan: str,
            _payload: str,
        ) -> None:
            notify_event.set()

        await conn.add_listener(channel, _on_notify)
        self._listen_conn = conn
        self._listen_channel = channel
        self._listen_callback = _on_notify
        logger.info(
            "Outbox LISTEN enabled channel=%s topic=%s",
            channel,
            self.topic,
        )

    async def _close_listen(self) -> None:
        conn = self._listen_conn
        channel = self._listen_channel
        callback = self._listen_callback
        self._listen_conn = None
        self._listen_channel = None
        self._listen_callback = None
        if conn is None:
            return
        try:
            if channel is not None and callback is not None and not conn.is_closed():
                with suppress(Exception):
                    await conn.remove_listener(channel, callback)
        finally:
            if not conn.is_closed():
                with suppress(Exception):
                    await conn.close()

    async def _await_first_or_timeout(
        self,
        waiters: list[asyncio.Task[bool]],
        *,
        wait_timeout: float,
    ) -> None:
        try:
            done, pending = await asyncio.wait(
                waiters,
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with suppress(asyncio.CancelledError, Exception):
                    task.result()
        except asyncio.CancelledError:
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            raise

    async def _idle_wait(self) -> None:
        """Wait for shutdown, optional NOTIFY, or safety-poll timeout."""
        if self._shutdown.is_set():
            return

        if self._wake_enabled:
            await self._ensure_listen()

        listeners_ok = (
            self._wake_enabled
            and self._listen_conn is not None
            and not self._listen_conn.is_closed()
        )
        if listeners_ok:
            self._notify_event.clear()

        waiters: list[asyncio.Task[bool]] = [
            asyncio.create_task(self._shutdown.wait(), name="outbox-shutdown-wait"),
        ]
        if listeners_ok:
            waiters.append(
                asyncio.create_task(self._notify_event.wait(), name="outbox-notify-wait"),
            )
        await self._await_first_or_timeout(waiters, wait_timeout=self._poll_interval)

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
        """Handle a row already claimed (status IN_PROGRESS)."""
        async with self._semaphore:
            outbox_id = row["id"]
            payload = await asyncio.to_thread(_assemble_consumer_payload, row)

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

    async def _poll_and_dispatch(self) -> int:
        """Atomically claim PENDING rows (→ IN_PROGRESS) and spawn handlers.

        Returns the number of claimed rows. Claim and status flip share one
        transaction so drain-on-claim cannot double-dispatch the same row.
        """
        async with in_transaction() as conn:
            sql = """
                SELECT id, payload, trace_context, namespace, saga_trace_id, step_span_id, event_type
                FROM outbox_events
                WHERE destination_topic = $1 AND status = $2
                ORDER BY created_at
                LIMIT $3
                FOR UPDATE SKIP LOCKED
            """
            rows = await conn.execute_query_dict(
                sql,
                [self.topic, OutboxStatus.PENDING.value, self._batch_size],
            )
            if not rows:
                return 0
            ids = [row["id"] for row in rows]
            now = utc_now()
            await (
                OutboxEvent.filter(id__in=ids)
                .using_db(conn)
                .update(status=OutboxStatus.IN_PROGRESS, updated_at=now)
            )

        for row in rows:
            if self._shutdown.is_set():
                break
            task = asyncio.create_task(self._process_row(row))
            self._track_task(task)
        return len(rows)
