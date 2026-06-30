"""Unit tests for common.outbox_reap_loop background daemon."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from common.outbox_reap_loop import run_outbox_reap_loop


@pytest.fixture
def fast_outbox_reap_settings(mocker):
    settings = mocker.MagicMock()
    settings.outbox_reap_interval_seconds = 0.01
    mocker.patch("common.outbox_reap_loop.get_settings", return_value=settings)
    return settings


@pytest.mark.asyncio
async def test_run_outbox_reap_loop_exits_on_shutdown(mocker, fast_outbox_reap_settings):
    mock_tick = mocker.patch(
        "common.outbox_reap_loop.run_outbox_maintenance_tick",
        new_callable=AsyncMock,
        return_value=0,
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_outbox_reap_loop(shutdown))
    await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert mock_tick.await_count >= 1


@pytest.mark.asyncio
async def test_run_outbox_reap_loop_engine_only_uses_engine_tick(mocker, fast_outbox_reap_settings):
    mock_engine_tick = mocker.patch(
        "common.outbox_reap_loop.run_engine_outbox_maintenance_tick",
        new_callable=AsyncMock,
        return_value=0,
    )
    mock_full_tick = mocker.patch(
        "common.outbox_reap_loop.run_outbox_maintenance_tick",
        new_callable=AsyncMock,
        return_value=0,
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_outbox_reap_loop(shutdown, engine_only=True))
    await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert mock_engine_tick.await_count >= 1
    mock_full_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_outbox_reap_loop_continues_after_tick_failure(mocker, fast_outbox_reap_settings):
    mock_tick = mocker.patch(
        "common.outbox_reap_loop.run_outbox_maintenance_tick",
        new_callable=AsyncMock,
        side_effect=[RuntimeError("db blip"), 0],
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_outbox_reap_loop(shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert mock_tick.await_count >= 2
