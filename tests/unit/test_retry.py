"""Unit tests for common.retry."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from common.retry import _PROVIDER_HINT_JITTER_S, _compute_delay_s, _jitter_delay_s, retry_async


@pytest.mark.asyncio
async def test_retry_async_succeeds_on_first_attempt():
    operation = AsyncMock(return_value="ok")
    result = await retry_async(
        operation,
        is_retryable=lambda _exc: True,
        max_attempts=3,
        base_delay_s=1.0,
        max_delay_s=60.0,
        sleep=AsyncMock(),
    )
    assert result == "ok"
    operation.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_async_succeeds_on_third_attempt():
    operation = AsyncMock(
        side_effect=[ConnectionError("down"), ConnectionError("still down"), "ok"],
    )
    sleep = AsyncMock()
    result = await retry_async(
        operation,
        is_retryable=lambda exc: isinstance(exc, ConnectionError),
        max_attempts=3,
        base_delay_s=1.0,
        max_delay_s=60.0,
        sleep=sleep,
    )
    assert result == "ok"
    assert operation.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_retry_async_non_retryable_fails_immediately():
    operation = AsyncMock(side_effect=ValueError("bad input"))
    sleep = AsyncMock()
    with pytest.raises(ValueError, match="bad input"):
        await retry_async(
            operation,
            is_retryable=lambda _exc: False,
            max_attempts=3,
            base_delay_s=1.0,
            max_delay_s=60.0,
            sleep=sleep,
        )
    operation.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_async_exhausts_max_attempts():
    operation = AsyncMock(side_effect=TimeoutError("slow"))
    sleep = AsyncMock()
    with pytest.raises(TimeoutError, match="slow"):
        await retry_async(
            operation,
            is_retryable=lambda exc: isinstance(exc, TimeoutError),
            max_attempts=2,
            base_delay_s=1.0,
            max_delay_s=60.0,
            sleep=sleep,
        )
    assert operation.await_count == 2
    assert sleep.await_count == 1


def test_jitter_delay_within_bounds():
    for attempt in range(4):
        delay = _jitter_delay_s(attempt=attempt, base_delay_s=2.0, max_delay_s=10.0)
        cap = min(10.0, 2.0 * (2**attempt))
        assert 0 <= delay <= cap


def test_compute_delay_honors_provider_hint_floor():
    for _ in range(50):
        delay = _compute_delay_s(
            attempt=0,
            base_delay_s=1.0,
            max_delay_s=60.0,
            suggested_s=12.0,
        )
        assert 12.0 <= delay <= 12.0 + _PROVIDER_HINT_JITTER_S


def test_compute_delay_caps_provider_hint_at_max_delay():
    for _ in range(20):
        delay = _compute_delay_s(
            attempt=0,
            base_delay_s=1.0,
            max_delay_s=5.0,
            suggested_s=12.0,
        )
        assert delay == 5.0


@pytest.mark.asyncio
async def test_retry_async_sleeps_at_least_provider_hint():
    operation = AsyncMock(side_effect=[ConnectionError("rate"), "ok"])
    sleep = AsyncMock()
    result = await retry_async(
        operation,
        is_retryable=lambda exc: isinstance(exc, ConnectionError),
        max_attempts=3,
        base_delay_s=1.0,
        max_delay_s=60.0,
        sleep=sleep,
        suggested_delay_s=lambda _exc: 8.5,
    )
    assert result == "ok"
    sleep.assert_awaited_once()
    slept = sleep.await_args.args[0]
    assert 8.5 <= slept <= 8.5 + _PROVIDER_HINT_JITTER_S


@pytest.mark.asyncio
async def test_retry_async_rejects_invalid_max_attempts():
    with pytest.raises(ValueError, match="max_attempts"):
        await retry_async(
            AsyncMock(return_value="ok"),
            is_retryable=lambda _exc: True,
            max_attempts=0,
            base_delay_s=1.0,
            max_delay_s=60.0,
        )
