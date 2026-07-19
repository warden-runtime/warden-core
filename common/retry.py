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

# Extra jitter when honoring a provider wait hint (avoids synchronized retries).
_PROVIDER_HINT_JITTER_S = 0.25


def _jitter_delay_s(*, attempt: int, base_delay_s: float, max_delay_s: float) -> float:
    """Full jitter: uniform in [0, min(max_delay, base * 2^attempt)]."""
    cap = min(max_delay_s, base_delay_s * (2**attempt))
    return random.uniform(0, cap)  # noqa: S311


def _compute_delay_s(
    *,
    attempt: int,
    base_delay_s: float,
    max_delay_s: float,
    suggested_s: float | None,
) -> float:
    """
    Backoff delay for one retry.

    Without a provider hint: full jitter in [0, exponential cap].
    With a hint: sleep at least the suggested wait (plus small jitter), still
    capped by max_delay_s, and never below the jittered exponential floor.
    """
    jittered = _jitter_delay_s(
        attempt=attempt,
        base_delay_s=base_delay_s,
        max_delay_s=max_delay_s,
    )
    if suggested_s is None or suggested_s <= 0:
        return jittered
    floor = max(jittered, suggested_s)
    hint_jitter = random.uniform(0, _PROVIDER_HINT_JITTER_S)  # noqa: S311
    return min(max_delay_s, floor + hint_jitter)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[BaseException], bool],
    max_attempts: int,
    base_delay_s: float,
    max_delay_s: float,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    suggested_delay_s: Callable[[BaseException], float | None] | None = None,
) -> T:
    """
    Run operation with retries on retryable exceptions.

    Args:
        operation: Async callable invoked on each attempt.
        is_retryable: Returns True when the exception warrants another attempt.
        max_attempts: Total attempts (including the first).
        base_delay_s: Base delay for exponential backoff.
        max_delay_s: Hard cap on sleep duration (including provider wait hints).
        sleep: Injectable sleep (defaults to asyncio.sleep).
        suggested_delay_s: Optional extractor for provider wait hints (Retry-After,
            "try again in Xs", etc.). When present, sleep is at least that duration.

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

            hint = suggested_delay_s(exc) if suggested_delay_s is not None else None
            delay = _compute_delay_s(
                attempt=attempt - 1,
                base_delay_s=base_delay_s,
                max_delay_s=max_delay_s,
                suggested_s=hint,
            )
            logger.warning(
                "Retryable failure (attempt %d/%d, delay=%.3fs%s): %s: %s",
                attempt,
                max_attempts,
                delay,
                f", provider_hint={hint:.3f}s" if hint is not None else "",
                type(exc).__name__,
                exc,
            )
            await sleep_fn(delay)

    if last_exc is not None:
        raise last_exc
    msg = "retry_async exhausted without exception"
    raise RuntimeError(msg)
