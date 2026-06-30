"""Claim ownership checks for worker command idempotency rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from tortoise.backends.base.client import BaseDBAsyncClient

from common.models import ProcessedCommand

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of attempting to claim a worker command idempotency key."""

    claimed: bool
    claim_token: UUID | None = None
    handler_started_at: datetime | None = None


def _naive_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def log_claim_superseded(
    *,
    idempotency_key: str,
    claim_token: UUID,
    handler_started_at: datetime,
    claim_created_at: datetime | None,
) -> None:
    now = datetime.now(UTC)
    handler_start = _naive_to_utc(handler_started_at)
    execution_duration_s = (now - handler_start).total_seconds()
    claim_age_s = (
        (now - _naive_to_utc(claim_created_at)).total_seconds()
        if claim_created_at is not None
        else execution_duration_s
    )
    logger.warning(
        "claim_superseded idempotency_key=%s claim_token=%s execution_duration_s=%.1f claim_age_s=%.1f",
        idempotency_key,
        claim_token,
        execution_duration_s,
        claim_age_s,
    )


async def claim_still_owned(
    *,
    idempotency_key: str,
    claim_token: UUID,
    conn: BaseDBAsyncClient,
) -> ProcessedCommand | None:
    return (
        await ProcessedCommand.filter(
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            result_emitted=False,
        )
        .using_db(conn)
        .first()
    )


async def mark_claim_result_emitted(
    *,
    idempotency_key: str,
    claim_token: UUID,
    handler_started_at: datetime,
    conn: BaseDBAsyncClient,
) -> bool:
    """Mark claim finished. Returns False when a reaper or retry superseded this worker."""
    updated = (
        await ProcessedCommand.filter(
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            result_emitted=False,
        )
        .using_db(conn)
        .update(result_emitted=True)
    )
    if updated:
        return True
    row = await ProcessedCommand.filter(idempotency_key=idempotency_key).using_db(conn).first()
    log_claim_superseded(
        idempotency_key=idempotency_key,
        claim_token=claim_token,
        handler_started_at=handler_started_at,
        claim_created_at=row.created_at if row is not None else None,
    )
    return False


async def verify_claim_before_emit(
    *,
    idempotency_key: str,
    claim_token: UUID,
    handler_started_at: datetime,
    conn: BaseDBAsyncClient,
) -> bool:
    row = await claim_still_owned(
        idempotency_key=idempotency_key,
        claim_token=claim_token,
        conn=conn,
    )
    if row is None:
        log_claim_superseded(
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            claim_created_at=None,
        )
        return False
    return True
