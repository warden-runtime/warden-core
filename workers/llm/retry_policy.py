"""Transient error classification for LLM provider calls."""

from __future__ import annotations

import asyncio

_TRANSIENT_HTTP_STATUS = frozenset({429, 502, 503, 504})


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
