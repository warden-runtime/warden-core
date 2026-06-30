"""Provider-agnostic async retry with exponential backoff and full jitter."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _jitter_delay_s(*, attempt: int, base_delay_s: float, max_delay_s: float) -> float:
    """Full jitter: uniform in [0, min(max_delay, base * 2^attempt)]."""
    cap = min(max_delay_s, base_delay_s * (2**attempt))
    return random.uniform(0, cap)  # noqa: S311


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[BaseException], bool],
    max_attempts: int,
    base_delay_s: float,
    max_delay_s: float,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """
    Run operation with retries on retryable exceptions.

    Args:
        operation: Async callable invoked on each attempt.
        is_retryable: Returns True when the exception warrants another attempt.
        max_attempts: Total attempts (including the first).
        base_delay_s: Base delay for exponential backoff.
        max_delay_s: Upper bound on backoff delay before jitter.
        sleep: Injectable sleep (defaults to asyncio.sleep).

    Returns:
        Result of the first successful operation call.

    Raises:
        The last exception when all attempts are exhausted or the error is not retryable.
    """
    if max_attempts < 1:
        msg = "max_attempts must be >= 1"
        raise ValueError(msg)

    sleep_fn = sleep or asyncio.sleep
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except BaseException as exc:
            last_exc = exc
            if not is_retryable(exc) or attempt >= max_attempts:
                raise

            delay = _jitter_delay_s(
                attempt=attempt - 1,
                base_delay_s=base_delay_s,
                max_delay_s=max_delay_s,
            )
            logger.warning(
                "Retryable failure (attempt %d/%d, delay=%.3fs): %s: %s",
                attempt,
                max_attempts,
                delay,
                type(exc).__name__,
                exc,
            )
            await sleep_fn(delay)

    if last_exc is not None:
        raise last_exc
    msg = "retry_async exhausted without exception"
    raise RuntimeError(msg)
