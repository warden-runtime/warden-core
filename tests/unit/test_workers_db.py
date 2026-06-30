"""Unit tests for workers.db init_db retry wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from workers.db import init_db


@pytest.mark.asyncio
async def test_worker_init_db_connects_on_first_attempt(mocker):
    mock_init = mocker.patch("workers.db.Tortoise.init", new_callable=AsyncMock)
    mock_schema = mocker.patch("workers.db.assert_core_schema_ready", new_callable=AsyncMock)
    mocker.patch("workers.db.get_settings").return_value.db_url = "postgres://user:pass@dbhost/db"

    await init_db()

    mock_init.assert_awaited_once()
    mock_schema.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_init_db_retries_then_succeeds(mocker):
    mock_init = mocker.patch(
        "workers.db.Tortoise.init",
        new_callable=AsyncMock,
        side_effect=[ConnectionError("refused"), None],
    )
    mock_schema = mocker.patch("workers.db.assert_core_schema_ready", new_callable=AsyncMock)
    mocker.patch("workers.db.get_settings").return_value.db_url = "sqlite://:memory:"
    mocker.patch("workers.db.asyncio.sleep", new_callable=AsyncMock)

    await init_db()

    assert mock_init.await_count == 2
    mock_schema.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_init_db_records_failed_attempt_span(mocker):
    mock_init = mocker.patch(
        "workers.db.Tortoise.init",
        new_callable=AsyncMock,
        side_effect=[ConnectionError("refused"), None],
    )
    mocker.patch("workers.db.assert_core_schema_ready", new_callable=AsyncMock)
    mocker.patch("workers.db.get_settings").return_value.db_url = "sqlite://:memory:"
    mocker.patch("workers.db.asyncio.sleep", new_callable=AsyncMock)

    mock_span = mocker.MagicMock()
    mock_tracer = mocker.MagicMock()
    mock_tracer.start_as_current_span.return_value.__enter__ = mocker.MagicMock(
        return_value=mock_span
    )
    mock_tracer.start_as_current_span.return_value.__exit__ = mocker.MagicMock(return_value=False)
    mocker.patch("workers.db.tracer", mock_tracer)

    await init_db()

    mock_tracer.start_as_current_span.assert_called_with("worker.db.connect_attempt")
    mock_span.record_exception.assert_called_once()
    assert mock_init.await_count == 2
