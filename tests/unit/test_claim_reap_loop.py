"""Unit tests for workers.claim_reap background loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from workers.claim_reap import run_claim_reap_loop


@pytest.fixture
def fast_reap_settings(mocker):
    settings = mocker.MagicMock()
    settings.processed_command_reap_interval_seconds = 0.01
    mocker.patch("workers.claim_reap.get_settings", return_value=settings)
    return settings


@pytest.mark.asyncio
async def test_run_claim_reap_loop_exits_on_shutdown(mocker, fast_reap_settings):
    mock_reap = mocker.patch(
        "workers.claim_reap.reap_stale_processed_commands",
        new_callable=AsyncMock,
        return_value=0,
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_claim_reap_loop(shutdown))
    await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert mock_reap.await_count >= 1


@pytest.mark.asyncio
async def test_run_claim_reap_loop_continues_after_reap_failure(mocker, fast_reap_settings):
    mock_reap = mocker.patch(
        "workers.claim_reap.reap_stale_processed_commands",
        new_callable=AsyncMock,
        side_effect=[RuntimeError("db blip"), 0],
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_claim_reap_loop(shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert mock_reap.await_count >= 2
