"""Unit tests for shared LangChain message content helpers."""

import pytest
from workers.llm.message_content import flatten_aimessage_content


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("  hi  ", "hi"),
        ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "a\nb"),
        ([{"type": "tool_use", "id": "1", "name": "x", "input": {}}], None),
        (["plain", {"type": "text", "text": "block"}], "plain\nblock"),
    ],
)
def test_flatten_aimessage_content(raw, expected):
    assert flatten_aimessage_content(raw) == expected
