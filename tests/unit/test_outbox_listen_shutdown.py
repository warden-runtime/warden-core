"""Unit tests for LISTEN idle-wait shutdown cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from common.messaging.postgres import PostgresQueueConsumer


@pytest.mark.asyncio
async def test_stop_unblocks_idle_wait_and_closes_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() must wake idle wait; start() finally must close the LISTEN connection."""
    closed = asyncio.Event()
    mock_conn = MagicMock()
    mock_conn.is_closed.return_value = False
    mock_conn.add_listener = AsyncMock()
    mock_conn.remove_listener = AsyncMock()

    async def _close() -> None:
        closed.set()

    mock_conn.close = AsyncMock(side_effect=_close)

    monkeypatch.setattr(
        "common.messaging.postgres.asyncpg.connect",
        AsyncMock(return_value=mock_conn),
    )
    monkeypatch.setattr(
        "common.messaging.postgres.get_settings",
        lambda: MagicMock(db_url="postgresql://u:p@localhost/db"),
    )

    async def handler(_payload: dict) -> None:
        return None

    consumer = PostgresQueueConsumer(
        topic="worker-commands",
        group_id="g",
        handler=handler,
        poll_interval=30.0,
        wake_enabled=True,
    )

    async def empty_poll(_self: PostgresQueueConsumer) -> int:
        return 0

    monkeypatch.setattr(PostgresQueueConsumer, "_poll_and_dispatch", empty_poll)

    task = asyncio.create_task(consumer.start())
    await asyncio.sleep(0.05)
    assert consumer._listen_conn is mock_conn
    await consumer.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await asyncio.wait_for(closed.wait(), timeout=1.0)
    mock_conn.remove_listener.assert_awaited()
    mock_conn.close.assert_awaited()
    assert consumer._listen_conn is None
