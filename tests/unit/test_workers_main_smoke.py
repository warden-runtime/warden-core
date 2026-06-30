"""Smoke tests for workers.main daemon wiring and shutdown."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from workers.main import main


async def _wait_for_shutdown(shutdown: asyncio.Event, **kwargs: object) -> None:
    await shutdown.wait()


@pytest.fixture
def worker_boot_mocks(mocker):
    mocker.patch("workers.main.setup_telemetry")
    mocker.patch("workers.main.configure_logging")
    mocker.patch("common.plugins.loader.load_plugins_from_env")
    mocker.patch("workers.main.wire_messaging_from_registry")
    mocker.patch("common.prompts.validate_prompts_root_if_configured")
    mocker.patch("workers.main.init_db", new_callable=AsyncMock)

    mock_consumer = mocker.MagicMock()

    async def _block_consumer() -> None:
        await asyncio.Event().wait()

    mock_consumer.start = AsyncMock(side_effect=_block_consumer)
    mock_consumer.stop = AsyncMock()
    mocker.patch(
        "workers.main.get_registry"
    ).return_value.messaging.create_consumer.return_value = mock_consumer

    mock_claim_reap = mocker.patch(
        "workers.main.run_claim_reap_loop",
        new_callable=AsyncMock,
        side_effect=_wait_for_shutdown,
    )
    mock_outbox_reap = mocker.patch(
        "workers.main.run_outbox_reap_loop",
        new_callable=AsyncMock,
        side_effect=_wait_for_shutdown,
    )
    return mock_consumer, mock_claim_reap, mock_outbox_reap


@pytest.mark.asyncio
async def test_worker_main_init_db_and_starts_background_loops(worker_boot_mocks, mocker):
    mock_consumer, mock_claim_reap, mock_outbox_reap = worker_boot_mocks
    mock_init_db = mocker.patch("workers.main.init_db", new_callable=AsyncMock)

    task = asyncio.create_task(main())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait_for(task, timeout=2.0)

    mock_init_db.assert_awaited_once()
    mock_consumer.start.assert_awaited()
    mock_claim_reap.assert_awaited_once()
    mock_outbox_reap.assert_awaited_once()
    mock_consumer.stop.assert_awaited()


@pytest.mark.asyncio
async def test_worker_main_reaper_crash_still_stops_consumer(mocker):
    """Background reaper failure must signal shutdown and always call consumer.stop()."""
    mocker.patch("workers.main.setup_telemetry")
    mocker.patch("workers.main.configure_logging")
    mocker.patch("common.plugins.loader.load_plugins_from_env")
    mocker.patch("workers.main.wire_messaging_from_registry")
    mocker.patch("common.prompts.validate_prompts_root_if_configured")
    mocker.patch("workers.main.init_db", new_callable=AsyncMock)

    shutdown_holder: dict[str, asyncio.Event] = {}

    async def _crash_reaper(shutdown: asyncio.Event) -> None:
        shutdown_holder["event"] = shutdown
        raise RuntimeError("reaper died")

    mocker.patch("workers.main.run_claim_reap_loop", side_effect=_crash_reaper)
    mocker.patch(
        "workers.main.run_outbox_reap_loop",
        new_callable=AsyncMock,
        side_effect=_wait_for_shutdown,
    )

    mock_consumer = mocker.MagicMock()

    async def _consumer_until_shutdown() -> None:
        for _ in range(200):
            evt = shutdown_holder.get("event")
            if evt is not None:
                await evt.wait()
                return
            await asyncio.sleep(0.01)

    mock_consumer.start = AsyncMock(side_effect=_consumer_until_shutdown)
    mock_consumer.stop = AsyncMock()
    mocker.patch(
        "workers.main.get_registry"
    ).return_value.messaging.create_consumer.return_value = mock_consumer

    await asyncio.wait_for(main(), timeout=2.0)

    assert shutdown_holder["event"].is_set()
    mock_consumer.stop.assert_awaited_once()
