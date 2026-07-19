"""Transient error classification and provider wait-hint parsing for LLM calls."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_TRANSIENT_HTTP_STATUS = frozenset({429, 502, 503, 504})

# OpenAI: "Please try again in 1.234s." / "Please try again in 758ms."
_TRY_AGAIN_IN_RE = re.compile(
    r"(?:please\s+)?try\s+again\s+in\s+([\d.]+)\s*(ms|milliseconds?|s|seconds?)?\b",
    re.IGNORECASE,
)


def _http_status_from_exception(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status
    return None


def _is_openai_transient(exc: BaseException) -> bool:
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
    except ImportError:
        return False

    return isinstance(
        exc,
        (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError),
    )


def is_transient_llm_error(exc: BaseException) -> bool:
    """
    Return True when an LLM ainvoke failure is likely transient and safe to retry.

    Does not retry validation, client errors (except 429), or governance failures.
    """
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True

    if _is_openai_transient(exc):
        return True

    status = _http_status_from_exception(exc)
    if status is not None:
        return status in _TRANSIENT_HTTP_STATUS

    return False


def _parse_retry_after_header(value: str) -> float | None:
    """Parse Retry-After as delta-seconds or HTTP-date; return seconds, or None."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        seconds = float(stripped)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if seconds >= 0 else None
    try:
        when = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - datetime.now(UTC)).total_seconds()
    return delta if delta > 0 else None


def _retry_after_from_exception(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw: object | None
    if hasattr(headers, "get"):
        raw = headers.get("Retry-After")
        if raw is None:
            raw = headers.get("retry-after")
    else:
        return None
    if raw is None:
        return None
    return _parse_retry_after_header(str(raw))


def _try_again_in_from_text(text: str) -> float | None:
    match = _TRY_AGAIN_IN_RE.search(text)
    if match is None:
        return None
    try:
        amount = float(match.group(1))
    except ValueError:
        return None
    if amount < 0:
        return None
    unit = (match.group(2) or "s").lower()
    if unit.startswith("ms") or unit.startswith("millisecond"):
        return amount / 1000.0
    return amount


def _append_nonempty_str(texts: list[str], value: object) -> None:
    if isinstance(value, str) and value:
        texts.append(value)


def _append_body_texts(texts: list[str], body: object) -> None:
    if isinstance(body, str):
        _append_nonempty_str(texts, body)
        return
    if not isinstance(body, dict):
        return
    err = body.get("error")
    if isinstance(err, dict):
        _append_nonempty_str(texts, err.get("message"))
    else:
        _append_nonempty_str(texts, err)
    _append_nonempty_str(texts, body.get("message"))


def _message_texts_from_exception(exc: BaseException) -> list[str]:
    texts: list[str] = []
    _append_nonempty_str(texts, str(exc))
    _append_body_texts(texts, getattr(exc, "body", None))
    return texts


def suggested_retry_delay_s(exc: BaseException) -> float | None:
    """
    Extract a provider-suggested wait (seconds) from a rate-limit / transient error.

    Prefers the larger of Retry-After and any "try again in Xs" message hint.
    Returns None when no usable hint is present.
    """
    candidates: list[float] = []
    header_hint = _retry_after_from_exception(exc)
    if header_hint is not None:
        candidates.append(header_hint)
    for text in _message_texts_from_exception(exc):
        msg_hint = _try_again_in_from_text(text)
        if msg_hint is not None:
            candidates.append(msg_hint)
    if not candidates:
        return None
    return max(candidates)
