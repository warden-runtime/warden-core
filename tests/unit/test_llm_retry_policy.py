"""Unit tests for workers.llm.retry_policy."""

from __future__ import annotations

import pytest
from workers.llm.retry_policy import is_transient_llm_error


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _NestedStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = type("Resp", (), {"status_code": status_code})()


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
    from unittest.mock import MagicMock

    openai = pytest.importorskip("openai")
    exc = openai.RateLimitError(
        message="rate limited",
        response=MagicMock(status_code=429, request=MagicMock()),
        body=None,
    )
    assert is_transient_llm_error(exc) is True
