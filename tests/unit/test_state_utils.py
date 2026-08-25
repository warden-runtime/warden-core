"""Unit tests for workers.adapters.state_utils (kernel transcript helpers)."""

from __future__ import annotations

from workers.adapters.state_utils import parse_json_object_from_assistant_text


def test_parse_json_object_from_assistant_text_strips_fence():
    assert parse_json_object_from_assistant_text('```json\n{"summary": "x"}\n```') == {
        "summary": "x"
    }
