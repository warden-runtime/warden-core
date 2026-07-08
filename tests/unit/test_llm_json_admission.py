"""Unit tests for LLM JSON admission (coerce + validate)."""

from __future__ import annotations

import pytest
from common.governance import admit_and_validate
from common.utils import coerce_llm_json_from_schema, coerce_tool_args_from_schema
from jsonschema import ValidationError

_FEASIBLE_SCHEMA = {
    "type": "object",
    "properties": {
        "feasible": {"type": "boolean"},
        "file_path": {"type": "string"},
    },
    "required": ["feasible", "file_path"],
}

_TAGS_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {"type": "array", "items": {"type": "string"}},
        "meta": {"type": "object"},
        "note": {"type": "string"},
    },
}


def test_admit_and_validate_coerces_stringified_boolean():
    admitted = admit_and_validate(
        {"feasible": "false", "file_path": "README.md"},
        _FEASIBLE_SCHEMA,
        "test",
    )
    assert admitted == {"feasible": False, "file_path": "README.md"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("1", True),
        ("0", False),
        ("false", False),
    ],
)
def test_admit_and_validate_boolean_string_variants(raw: str, expected: bool):
    admitted = admit_and_validate(
        {"feasible": raw, "file_path": "x"},
        _FEASIBLE_SCHEMA,
        "test",
    )
    assert admitted["feasible"] is expected


def test_admit_and_validate_stringified_array_and_object():
    admitted = admit_and_validate(
        {"tags": '["a","b"]', "meta": '{"k":1}', "note": "ok"},
        _TAGS_SCHEMA,
        "test",
    )
    assert admitted == {"tags": ["a", "b"], "meta": {"k": 1}, "note": "ok"}


def test_admit_and_validate_string_field_never_json_parsed():
    raw = '{"x":1}'
    admitted = admit_and_validate({"note": raw}, _TAGS_SCHEMA, "test")
    assert admitted["note"] == raw


def test_admit_and_validate_uncoerceable_garbage_still_raises():
    with pytest.raises(ValidationError):
        admit_and_validate(
            {"feasible": "not-a-bool", "file_path": "x"},
            _FEASIBLE_SCHEMA,
            "test",
        )


def test_coerce_tool_args_alias_matches_llm_json_name():
    assert coerce_tool_args_from_schema is coerce_llm_json_from_schema
    args = {"commands": '["echo"]'}
    schema = {
        "type": "object",
        "properties": {"commands": {"type": "array", "items": {"type": "string"}}},
    }
    assert coerce_tool_args_from_schema(args, schema) == {"commands": ["echo"]}
