"""Unit tests for engine.db init_db retry wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from engine.db import init_db


@pytest.mark.asyncio
async def test_engine_init_db_connects_on_first_attempt(mocker):
    mock_init = mocker.patch("engine.db.Tortoise.init", new_callable=AsyncMock)
    mock_schema = mocker.patch("engine.db.assert_core_schema_ready", new_callable=AsyncMock)
    mocker.patch("engine.db.get_settings").return_value.db_url = "sqlite://:memory:"

    await init_db()

    mock_init.assert_awaited_once()
    mock_schema.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_init_db_retries_then_succeeds(mocker):
    mock_init = mocker.patch(
        "engine.db.Tortoise.init",
        new_callable=AsyncMock,
        side_effect=[ConnectionError("refused"), None],
    )
    mock_schema = mocker.patch("engine.db.assert_core_schema_ready", new_callable=AsyncMock)
    mocker.patch("engine.db.get_settings").return_value.db_url = "sqlite://:memory:"
    mocker.patch("engine.db.asyncio.sleep", new_callable=AsyncMock)

    await init_db()

    assert mock_init.await_count == 2
    mock_schema.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_init_db_raises_after_max_retries(mocker):
    mock_init = mocker.patch(
        "engine.db.Tortoise.init",
        new_callable=AsyncMock,
        side_effect=ConnectionError("refused"),
    )
    mocker.patch("engine.db.assert_core_schema_ready", new_callable=AsyncMock)
    mocker.patch("engine.db.get_settings").return_value.db_url = "sqlite://:memory:"
    mocker.patch("engine.db.asyncio.sleep", new_callable=AsyncMock)

    with pytest.raises(ConnectionError, match="refused"):
        await init_db()

    assert mock_init.await_count == 15
