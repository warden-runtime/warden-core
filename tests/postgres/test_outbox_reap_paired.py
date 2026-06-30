"""Paired outbox reap against real PostgreSQL (FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from common.models import OutboxEvent, OutboxStatus, ProcessedCommand
from common.outbox_reap import reap_paired_worker_commands_outbox, stale_outbox_cutoff
from common.topics import TOPIC_WORKER_COMMANDS

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_paired_reap_evicts_claim_without_created_at_cutoff():
    """Outbox stale by TTL but claim younger than worker TTL still gets evicted."""
    idem = "paired-idem-key"
    await ProcessedCommand.create(
        idempotency_key=idem,
        namespace="default",
        result_emitted=False,
    )
    row = await OutboxEvent.create(
        namespace="default",
        saga_trace_id="c" * 32,
        step_span_id="d" * 16,
        event_type="DO_STEP",
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=idem,
        payload={"type": "DO_STEP", "idempotency_key": idem},
        status=OutboxStatus.IN_PROGRESS,
    )
    await OutboxEvent.filter(id=row.id).update(
        updated_at=stale_outbox_cutoff() - timedelta(seconds=60),
    )
    reaped = await reap_paired_worker_commands_outbox(TOPIC_WORKER_COMMANDS)
    assert reaped == 1
    await row.refresh_from_db()
    assert row.status == OutboxStatus.PENDING
    assert await ProcessedCommand.filter(idempotency_key=idem).count() == 0


@pytest.mark.asyncio
async def test_paired_reap_skip_locked_single_row():
    """Concurrent reaps must not double-reset the same stale IN_PROGRESS row."""
    idem = "skip-locked-idem"
    row = await OutboxEvent.create(
        namespace="default",
        saga_trace_id="e" * 32,
        step_span_id="f" * 16,
        event_type="DO_STEP",
        destination_topic=TOPIC_WORKER_COMMANDS,
        idempotency_key=idem,
        payload={"type": "DO_STEP", "idempotency_key": idem},
        status=OutboxStatus.IN_PROGRESS,
    )
    await OutboxEvent.filter(id=row.id).update(
        updated_at=stale_outbox_cutoff() - timedelta(seconds=30),
    )

    reaped_a, reaped_b = await asyncio.gather(
        reap_paired_worker_commands_outbox(TOPIC_WORKER_COMMANDS),
        reap_paired_worker_commands_outbox(TOPIC_WORKER_COMMANDS),
    )
    assert reaped_a + reaped_b == 1
    await row.refresh_from_db()
    assert row.status == OutboxStatus.PENDING
