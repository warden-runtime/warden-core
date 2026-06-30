"""Unit tests for common.tool_results LLM clipping helpers."""

from __future__ import annotations

import json

import pytest
from common.tool_results import (
    DEFAULT_TOOL_MESSAGE_LIMIT,
    clip_tool_text_for_llm,
    resolve_tool_message_limit,
)


def test_clip_tool_text_for_llm_short_text_unchanged() -> None:
    text = '{"totalCount": 0}'
    assert clip_tool_text_for_llm(text, limit=8000) == text


def test_clip_tool_text_for_llm_non_json_plain_truncate() -> None:
    text = "x" * 9000
    clipped = clip_tool_text_for_llm(text, limit=100)
    assert len(clipped) <= 100
    assert "WARDEN: tool output truncated for LLM context" in clipped


def test_clip_tool_text_for_llm_json_preserves_total_count() -> None:
    issues = [{"body": "a" * 2000} for _ in range(10)]
    payload = json.dumps({"totalCount": 10, "issues": issues})
    assert len(payload) > DEFAULT_TOOL_MESSAGE_LIMIT
    clipped = clip_tool_text_for_llm(payload, limit=DEFAULT_TOOL_MESSAGE_LIMIT)
    parsed = json.loads(clipped)
    assert parsed["totalCount"] == 10
    assert parsed.get("_warden_clipped") is True
    assert len(parsed["issues"]) < len(issues)
    assert len(clipped) <= DEFAULT_TOOL_MESSAGE_LIMIT


def test_resolve_tool_message_limit_zero_disables() -> None:
    assert resolve_tool_message_limit(0) is None
    assert resolve_tool_message_limit(-1) is None


def test_resolve_tool_message_limit_positive() -> None:
    assert resolve_tool_message_limit(12000) == 12000


def test_clip_tool_text_for_llm_limit_zero_passthrough() -> None:
    text = "y" * 9000
    assert clip_tool_text_for_llm(text, limit=0) == text


@pytest.mark.parametrize("raw", ['{"status": "failed to locate server"}'])
def test_clip_small_json_object_unchanged_when_under_limit(raw: str) -> None:
    assert clip_tool_text_for_llm(raw, limit=8000) == raw


def test_clip_json_zero_match_empty_issues_unchanged() -> None:
    """MCP zero-match payloads with empty top-level arrays must not raise."""
    payload = json.dumps(
        {
            "totalCount": 0,
            "issues": [],
            "pageInfo": {"hasNextPage": False},
        }
    )
    assert clip_tool_text_for_llm(payload, limit=DEFAULT_TOOL_MESSAGE_LIMIT) == payload


def test_clip_json_empty_issues_with_oversized_scalar() -> None:
    payload = json.dumps(
        {
            "totalCount": 0,
            "issues": [],
            "notes": "n" * 9000,
        }
    )
    clipped = clip_tool_text_for_llm(payload, limit=DEFAULT_TOOL_MESSAGE_LIMIT)
    parsed = json.loads(clipped)
    assert parsed["totalCount"] == 0
    assert parsed["issues"] == []
    assert len(clipped) <= DEFAULT_TOOL_MESSAGE_LIMIT
