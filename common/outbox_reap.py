"""Reap stale IN_PROGRESS outbox rows for redelivery."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from tortoise.transactions import in_transaction

from common.config import get_settings
from common.models import OutboxEvent, OutboxStatus, ProcessedCommand
from common.outbox_timestamps import utc_now

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient

logger = logging.getLogger(__name__)


def stale_outbox_cutoff(*, now: datetime | None = None) -> datetime:
    settings = get_settings()
    moment = now if now is not None else datetime.now(UTC)
    return moment - timedelta(seconds=settings.outbox_stale_in_progress_seconds)


def _row_updated_at(row: dict[str, Any]) -> datetime:
    value = row["updated_at"]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return datetime.fromisoformat(str(value))


def _still_stale(row: dict[str, Any], cutoff: datetime) -> bool:
    return _row_updated_at(row) < cutoff


async def reap_stale_in_progress_outbox_rows(
    topic: str,
    *,
    batch_size: int | None = None,
    conn: BaseDBAsyncClient | None = None,
) -> int:
    """Reset stale IN_PROGRESS rows to PENDING (engine-events; no paired claims)."""
    settings = get_settings()
    limit = batch_size if batch_size is not None else settings.outbox_reap_batch_size
    cutoff = stale_outbox_cutoff()
    now = utc_now()
    q = OutboxEvent.filter(
        destination_topic=topic,
        status=OutboxStatus.IN_PROGRESS,
        updated_at__lt=cutoff,
    )
    if conn is not None:
        q = q.using_db(conn)
    updated = await q.limit(limit).update(status=OutboxStatus.PENDING, updated_at=now)
    if updated:
        logger.warning(
            "Reaped %d stale IN_PROGRESS outbox row(s) topic=%s older than %s",
            updated,
            topic,
            cutoff.isoformat(),
        )
    return updated


async def reap_paired_worker_commands_outbox(
    topic: str,
    *,
    batch_size: int | None = None,
) -> int:
    """Paired reap: one transaction per row; unconditional claim evict by idempotency_key."""
    settings = get_settings()
    limit = batch_size if batch_size is not None else settings.outbox_reap_batch_size
    cutoff = stale_outbox_cutoff()
    reaped = 0
    for _ in range(limit):
        async with in_transaction() as conn:
            rows = await conn.execute_query_dict(
                """
                SELECT id, idempotency_key, status, updated_at
                FROM outbox_events
                WHERE destination_topic = $1
                  AND status = $2
                  AND updated_at < $3
                ORDER BY updated_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                [topic, OutboxStatus.IN_PROGRESS.value, cutoff],
            )
            if not rows:
                break
            candidate = rows[0]
            if not _still_stale(candidate, cutoff):
                continue
            key = candidate.get("idempotency_key")
            if key:
                await (
                    ProcessedCommand.filter(
                        idempotency_key=key,
                        result_emitted=False,
                    )
                    .using_db(conn)
                    .delete()
                )
            now = utc_now()
            await (
                OutboxEvent.filter(id=candidate["id"])
                .using_db(conn)
                .update(
                    status=OutboxStatus.PENDING,
                    updated_at=now,
                )
            )
            reaped += 1
            logger.warning(
                "Paired reap reset outbox id=%s topic=%s idempotency_key=%s to PENDING",
                candidate["id"],
                topic,
                key,
            )
    return reaped


async def run_outbox_maintenance_tick(
    *, worker_commands_topic: str, engine_events_topic: str
) -> int:
    """Worker process: orphan claims handled separately; paired reap for worker-commands."""
    from common.processed_command_reap import reap_stale_processed_commands

    orphan_deleted = await reap_stale_processed_commands()
    if orphan_deleted:
        logger.info("Outbox maintenance removed %d orphan ProcessedCommand row(s)", orphan_deleted)
    paired = await reap_paired_worker_commands_outbox(worker_commands_topic)
    return paired


async def run_engine_outbox_maintenance_tick(*, engine_events_topic: str) -> int:
    """Engine process: reap stale IN_PROGRESS rows on engine-events only."""
    return await reap_stale_in_progress_outbox_rows(engine_events_topic)
