from datetime import UTC, datetime, timedelta

import pytest
from common.contracts import CommandType
from common.models import ProcessedCommand
from common.plugins.context import ExecutionScope
from common.processed_command_reap import (
    claim_is_stale,
    reap_stale_claim_by_key,
    reap_stale_processed_commands,
    release_worker_claim_for_retry,
)
from workers.logic import _claim_idempotency_key


async def _old_claim(*, idempotency_key: str = "stale-idem", namespace: str = "default") -> None:
    await ProcessedCommand.create(
        idempotency_key=idempotency_key,
        namespace=namespace,
        result_emitted=False,
    )
    await ProcessedCommand.filter(idempotency_key=idempotency_key).update(
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )


def test_claim_is_stale_treats_naive_created_at_as_utc():
    cutoff = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    row = ProcessedCommand(
        idempotency_key="naive-ts",
        namespace="default",
        result_emitted=False,
        created_at=datetime(2024, 6, 1, 10, 0, 0),
    )
    assert claim_is_stale(row, cutoff=cutoff) is True


@pytest.mark.asyncio
async def test_release_worker_claim_for_retry_deletes_row():
    await ProcessedCommand.create(
        idempotency_key="hitl-release",
        namespace="default",
        result_emitted=True,
    )
    deleted = await release_worker_claim_for_retry("hitl-release")
    assert deleted == 1
    assert await ProcessedCommand.filter(idempotency_key="hitl-release").count() == 0


@pytest.mark.asyncio
async def test_release_worker_claim_for_retry_noop_when_missing():
    assert await release_worker_claim_for_retry("missing-key") == 0


@pytest.mark.asyncio
async def test_reap_stale_processed_commands_passes_batch_size(mocker):
    mock_filter = mocker.patch("common.processed_command_reap.ProcessedCommand.filter")
    chain = mock_filter.return_value
    chain.limit.return_value = chain
    chain.delete = mocker.AsyncMock(return_value=0)

    await reap_stale_processed_commands(batch_size=7)

    chain.limit.assert_called_once_with(7)
    chain.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_is_stale_respects_result_emitted():
    await ProcessedCommand.create(
        idempotency_key="done-idem",
        namespace="default",
        result_emitted=True,
    )
    row = await ProcessedCommand.get(idempotency_key="done-idem")
    assert claim_is_stale(row) is False


@pytest.mark.asyncio
async def test_reap_stale_claim_by_key_removes_old_unfinished_claim():
    await _old_claim(idempotency_key="reap-one")
    assert await reap_stale_claim_by_key("reap-one") is True
    assert await ProcessedCommand.filter(idempotency_key="reap-one").count() == 0


@pytest.mark.asyncio
async def test_reap_stale_claim_by_key_keeps_completed_claim():
    await _old_claim(idempotency_key="done-stale-age")
    await ProcessedCommand.filter(idempotency_key="done-stale-age").update(
        result_emitted=True,
    )
    assert await reap_stale_claim_by_key("done-stale-age") is False
    assert await ProcessedCommand.filter(idempotency_key="done-stale-age").count() == 1


@pytest.mark.asyncio
async def test_reap_stale_claim_by_key_keeps_fresh_claim():
    await ProcessedCommand.create(
        idempotency_key="fresh-idem",
        namespace="default",
        result_emitted=False,
    )
    assert await reap_stale_claim_by_key("fresh-idem") is False
    assert await ProcessedCommand.filter(idempotency_key="fresh-idem").count() == 1


@pytest.mark.asyncio
async def test_reap_stale_processed_commands_batch():
    await _old_claim(idempotency_key="batch-a")
    await _old_claim(idempotency_key="batch-b")
    await ProcessedCommand.create(
        idempotency_key="batch-fresh",
        namespace="default",
        result_emitted=False,
    )
    deleted = await reap_stale_processed_commands()
    assert deleted == 2
    assert await ProcessedCommand.filter(idempotency_key="batch-fresh").count() == 1


@pytest.mark.asyncio
async def test_reap_stale_processed_commands_skips_completed_in_batch():
    await _old_claim(idempotency_key="batch-done")
    await ProcessedCommand.filter(idempotency_key="batch-done").update(
        result_emitted=True,
    )
    deleted = await reap_stale_processed_commands()
    assert deleted == 0
    assert await ProcessedCommand.filter(idempotency_key="batch-done").count() == 1


@pytest.mark.asyncio
async def test_claim_idempotency_key_reclaims_after_stale_row():
    await _old_claim(idempotency_key="reclaim-idem")
    scope = ExecutionScope(
        namespace="default",
        trace_id="a" * 32,
        step_span_id="b" * 16,
        idempotency_key="reclaim-idem",
        command_type=CommandType.DO_STEP.value,
        worker_name="w",
    )

    claimed = await _claim_idempotency_key(
        idempotency_key="reclaim-idem",
        namespace="default",
        scope=scope,
    )
    assert claimed.claimed is True
    row = await ProcessedCommand.get(idempotency_key="reclaim-idem")
    assert row.result_emitted is False


@pytest.mark.asyncio
async def test_claim_idempotency_key_still_skips_completed_duplicate():
    await ProcessedCommand.create(
        idempotency_key="dup-idem",
        namespace="default",
        result_emitted=True,
    )
    scope = ExecutionScope(
        namespace="default",
        trace_id="a" * 32,
        step_span_id="b" * 16,
        idempotency_key="dup-idem",
        command_type=CommandType.DO_STEP.value,
        worker_name="w",
    )

    claimed = await _claim_idempotency_key(
        idempotency_key="dup-idem",
        namespace="default",
        scope=scope,
    )
    assert claimed.claimed is False
