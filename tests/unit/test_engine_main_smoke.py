"""Smoke tests for engine.main daemon wiring and teardown."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from engine.api.api import api_listening
from engine.main import _teardown_engine, main


@pytest.fixture(autouse=True)
def _reset_api_listening():
    api_listening.clear()
    yield
    api_listening.clear()


async def _wait_outbox_reap(shutdown: asyncio.Event, **kwargs: object) -> None:
    await shutdown.wait()


@pytest.fixture
def engine_boot_mocks(mocker):
    mocker.patch("common.plugins.loader.load_plugins_from_env")
    mocker.patch("engine.main.wire_messaging_from_registry")
    mocker.patch("engine.main.setup_telemetry")
    mocker.patch("engine.main.configure_logging")
    mocker.patch("common.prompts.validate_prompts_root_if_configured")

    async def _fake_api() -> None:
        from engine.api.api import notify_api_listening

        notify_api_listening("127.0.0.1", 8000)
        await asyncio.Event().wait()

    mocker.patch("engine.main.run_api_server", side_effect=_fake_api)

    mock_consumer = mocker.MagicMock()

    async def _block_consumer() -> None:
        await asyncio.Event().wait()

    mock_consumer.start = AsyncMock(side_effect=_block_consumer)
    mock_consumer.stop = AsyncMock()
    mocker.patch(
        "engine.main.get_registry"
    ).return_value.messaging.create_consumer.return_value = mock_consumer

    mock_reap = mocker.patch(
        "common.outbox_reap_loop.run_outbox_reap_loop",
        new_callable=AsyncMock,
        side_effect=_wait_outbox_reap,
    )
    return mock_consumer, mock_reap


@pytest.mark.asyncio
async def test_engine_main_wires_consumer_and_reap_after_api_listen(engine_boot_mocks):
    mock_consumer, mock_reap = engine_boot_mocks
    task = asyncio.create_task(main())
    await asyncio.wait_for(api_listening.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    mock_consumer.start.assert_awaited()
    mock_reap.assert_awaited_once()
    mock_consumer.stop.assert_awaited()


@pytest.mark.asyncio
async def test_teardown_engine_stops_consumer_and_cancels_tasks(mocker):
    mock_consumer = mocker.MagicMock()
    mock_consumer.stop = AsyncMock()

    async def _block() -> None:
        await asyncio.Event().wait()

    api_task = asyncio.create_task(_block())
    consumer_task = asyncio.create_task(_block())
    await asyncio.sleep(0)

    await _teardown_engine(
        consumer=mock_consumer,
        api_task=api_task,
        consumer_task=consumer_task,
    )
    mock_consumer.stop.assert_awaited_once()
    assert api_task.done()
    assert consumer_task.done()
