"""Unit tests for outbox stale-IN_PROGRESS reap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from common.models import OutboxEvent, OutboxStatus
from common.outbox_reap import reap_stale_in_progress_outbox_rows


@pytest.mark.asyncio
async def test_reap_stale_in_progress_outbox_rows_resets_to_pending():
    row = await OutboxEvent.create(
        namespace="default",
        saga_trace_id="a" * 32,
        step_span_id="b" * 16,
        event_type="DO_STEP",
        destination_topic="engine-events",
        idempotency_key="engine-stale",
        payload={"type": "DO_STEP"},
        status=OutboxStatus.IN_PROGRESS,
    )
    await OutboxEvent.filter(id=row.id).update(
        updated_at=datetime.now(UTC) - timedelta(hours=2),
    )
    deleted = await reap_stale_in_progress_outbox_rows("engine-events")
    assert deleted == 1
    await row.refresh_from_db()
    assert row.status == OutboxStatus.PENDING


@pytest.mark.asyncio
async def test_reap_skips_fresh_in_progress_rows():
    row = await OutboxEvent.create(
        namespace="default",
        saga_trace_id="a" * 32,
        step_span_id="b" * 16,
        event_type="DO_STEP",
        destination_topic="engine-events",
        idempotency_key="engine-fresh",
        payload={"type": "DO_STEP"},
        status=OutboxStatus.IN_PROGRESS,
    )
    deleted = await reap_stale_in_progress_outbox_rows("engine-events")
    assert deleted == 0
    await row.refresh_from_db()
    assert row.status == OutboxStatus.IN_PROGRESS
