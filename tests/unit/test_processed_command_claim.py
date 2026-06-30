"""Unit tests for worker claim_token fencing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from common.contracts import CommandType
from common.models import ProcessedCommand, WorkerDefinition
from common.plugins.context import ExecutionScope
from common.processed_command_claim import mark_claim_result_emitted
from tortoise.transactions import in_transaction


@pytest.mark.asyncio
async def test_mark_claim_result_emitted_wrong_token_returns_false():
    token = uuid.uuid4()
    await ProcessedCommand.create(
        idempotency_key="fence-key",
        namespace="default",
        claim_token=token,
        result_emitted=False,
    )
    async with in_transaction() as conn:
        ok = await mark_claim_result_emitted(
            idempotency_key="fence-key",
            claim_token=uuid.uuid4(),
            handler_started_at=datetime.now(UTC),
            conn=conn,
        )
    assert ok is False
    row = await ProcessedCommand.get(idempotency_key="fence-key")
    assert row.result_emitted is False


@pytest.mark.asyncio
async def test_finalize_success_skips_emit_when_superseded():
    from workers.logic import _finalize_success

    token = uuid.uuid4()
    other = uuid.uuid4()
    await ProcessedCommand.create(
        idempotency_key="superseded-finalize",
        namespace="default",
        claim_token=other,
        result_emitted=False,
    )
    scope = ExecutionScope(
        namespace="default",
        trace_id="a" * 32,
        step_span_id="b" * 16,
        idempotency_key="superseded-finalize",
        command_type=CommandType.DO_STEP.value,
        worker_name="w",
    )
    worker_def = WorkerDefinition(
        name="w",
        namespace="default",
        version="1.0.0",
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="x",
    )
    emit = AsyncMock()
    await _finalize_success(
        scope=scope,
        worker_definition=worker_def,
        idempotency_key="superseded-finalize",
        claim_token=token,
        handler_started_at=datetime.now(UTC) - timedelta(minutes=5),
        result_event_type="STEP_COMPLETED",
        output={},
        emit=emit,
    )
    emit.assert_not_awaited()
