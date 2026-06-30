"""Reap stale worker command claims so redelivery can reclaim idempotency keys."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from common.config import get_settings

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient
from common.models import ProcessedCommand

logger = logging.getLogger(__name__)

DEFAULT_REAP_BATCH_SIZE = 100


def stale_claim_cutoff(*, now: datetime | None = None) -> datetime:
    """Return created_at threshold: claims older than this with result_emitted=False are stale."""
    settings = get_settings()
    moment = now if now is not None else datetime.now(UTC)
    return moment - timedelta(seconds=settings.processed_command_stale_claim_seconds)


def claim_is_stale(
    row: ProcessedCommand,
    *,
    cutoff: datetime | None = None,
) -> bool:
    """True when the row is an unfinished claim older than the configured TTL."""
    if row.result_emitted:
        return False
    threshold = cutoff if cutoff is not None else stale_claim_cutoff()
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created < threshold


async def reap_stale_claim_by_key(idempotency_key: str) -> bool:
    """Delete one stale claim row if present. Returns True when a row was removed."""
    cutoff = stale_claim_cutoff()
    deleted = await ProcessedCommand.filter(
        idempotency_key=idempotency_key,
        result_emitted=False,
        created_at__lt=cutoff,
    ).delete()
    if deleted:
        logger.warning(
            "Reaped stale ProcessedCommand claim idempotency_key=%s older than %s",
            idempotency_key,
            cutoff.isoformat(),
        )
    return deleted > 0


async def release_worker_claim_for_retry(
    idempotency_key: str,
    *,
    conn: BaseDBAsyncClient | None = None,
) -> int:
    """Remove a worker ProcessedCommand row so HITL manual retry can re-deliver the step command."""
    q = ProcessedCommand.filter(idempotency_key=idempotency_key)
    if conn is not None:
        q = q.using_db(conn)
    deleted = await q.delete()
    if deleted:
        logger.info(
            "Released ProcessedCommand idempotency_key=%s for HITL retry",
            idempotency_key,
        )
    return deleted


async def reap_stale_processed_commands(
    *,
    batch_size: int = DEFAULT_REAP_BATCH_SIZE,
) -> int:
    """Delete up to batch_size stale claims. Returns number of rows removed."""
    cutoff = stale_claim_cutoff()
    deleted = (
        await ProcessedCommand.filter(
            result_emitted=False,
            created_at__lt=cutoff,
        )
        .limit(batch_size)
        .delete()
    )
    if deleted:
        logger.warning(
            "Reaped %d stale ProcessedCommand claim(s) older than %s",
            deleted,
            cutoff.isoformat(),
        )
    return deleted
