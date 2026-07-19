"""Unit tests for workers.llm.retry_policy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from workers.llm.retry_policy import is_transient_llm_error, suggested_retry_delay_s


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _NestedStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = type("Resp", (), {"status_code": status_code})()


class _HintError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retry_after: str | None = None,
        body: object | None = None,
    ) -> None:
        super().__init__(message)
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        self.response = type("Resp", (), {"status_code": 429, "headers": headers})()
        self.body = body


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("timeout"), True),
        (ConnectionError("conn"), True),
        (_StatusError(429), True),
        (_StatusError(503), True),
        (_NestedStatusError(504), True),
        (_StatusError(400), False),
        (_StatusError(404), False),
        (ValueError("bad"), False),
        (TypeError("wrong type"), False),
    ],
)
def test_is_transient_llm_error(exc: BaseException, expected: bool):
    assert is_transient_llm_error(exc) is expected


def test_is_transient_llm_error_openai_rate_limit_when_available():
    openai = pytest.importorskip("openai")
    exc = openai.RateLimitError(
        message="rate limited",
        response=MagicMock(status_code=429, request=MagicMock()),
        body=None,
    )
    assert is_transient_llm_error(exc) is True


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            _HintError(
                "Rate limit reached. Please try again in 1.234s.",
            ),
            1.234,
        ),
        (
            _HintError("Please try again in 758ms."),
            0.758,
        ),
        (
            _HintError("rate limited", retry_after="12"),
            12.0,
        ),
        (
            _HintError(
                "Please try again in 3s.",
                retry_after="10",
            ),
            10.0,
        ),
        (
            _HintError(
                "limited",
                body={"error": {"message": "Please try again in 5.5s."}},
            ),
            5.5,
        ),
        (_HintError("no hint here"), None),
        (ValueError("plain"), None),
    ],
)
def test_suggested_retry_delay_s(exc: BaseException, expected: float | None):
    assert suggested_retry_delay_s(exc) == expected


def test_suggested_retry_delay_s_openai_rate_limit_message():
    openai = pytest.importorskip("openai")
    response = MagicMock(status_code=429, request=MagicMock())
    response.headers = {"Retry-After": "2"}
    exc = openai.RateLimitError(
        message=(
            "Rate limit reached for gpt-4o on tokens per min (TPM): "
            "Limit 30000, Used 29900, Requested 500. Please try again in 8.421s."
        ),
        response=response,
        body=None,
    )
    assert suggested_retry_delay_s(exc) == 8.421
