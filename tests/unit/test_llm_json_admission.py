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


_MARSHALL_ARCHITECT_SCHEMA = {
    "type": "object",
    "properties": {
        "feasible": {"type": "boolean"},
        "clone_url": {"type": ["string", "null"]},
        "abandon_reason": {"type": ["string", "null"]},
    },
}

_GITHUB_TRIAGE_FRAGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "recommended_issue_number": {"type": ["integer", "null"], "minimum": 1},
        "comment_body": {"type": ["string", "null"], "minLength": 1},
    },
}


def test_coerce_nullable_union_marshall_architect_case():
    payload = {"feasible": "true", "clone_url": "null", "abandon_reason": "null"}
    expected = {"feasible": True, "clone_url": None, "abandon_reason": None}
    assert coerce_llm_json_from_schema(payload, _MARSHALL_ARCHITECT_SCHEMA) == expected
    assert admit_and_validate(payload, _MARSHALL_ARCHITECT_SCHEMA, "test") == expected


@pytest.mark.parametrize("raw_null", ["null", "None", " none ", "NULL\n"])
def test_coerce_nullable_union_string_null_variants(raw_null: str):
    schema = {"type": "object", "properties": {"field": {"type": ["string", "null"]}}}
    assert coerce_llm_json_from_schema({"field": raw_null}, schema) == {"field": None}


def test_coerce_nullable_union_integer_string_coerces():
    payload = {"recommended_issue_number": "42"}
    assert coerce_llm_json_from_schema(payload, _GITHUB_TRIAGE_FRAGMENT_SCHEMA) == {
        "recommended_issue_number": 42,
    }


def test_coerce_nullable_union_integer_null_string():
    payload = {"recommended_issue_number": "null"}
    assert coerce_llm_json_from_schema(payload, _GITHUB_TRIAGE_FRAGMENT_SCHEMA) == {
        "recommended_issue_number": None,
    }


def test_coerce_nullable_union_does_not_raise_type_error():
    schema = {"type": "object", "properties": {"x": {"type": ["string", "null"]}}}
    assert coerce_llm_json_from_schema({"x": "y"}, schema) == {"x": "y"}


def test_coerce_null_string_without_nullable_schema_stays_literal():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}
    assert coerce_llm_json_from_schema({"note": "null"}, schema) == {"note": "null"}
    assert coerce_llm_json_from_schema({"note": "None"}, schema) == {"note": "None"}


def test_coerce_path_symbol_object_to_string_locus():
    """GPT-style {path, symbol} objects admit into string fields (SWE architect locus)."""
    schema = {
        "type": "object",
        "properties": {
            "strategies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "locus": {"type": "string", "minLength": 1},
                    },
                    "required": ["name", "locus"],
                },
            }
        },
        "required": ["strategies"],
    }
    payload = {
        "strategies": [
            {
                "name": "A",
                "locus": {
                    "path": "astropy/modeling/separable.py",
                    "symbol": "_separable",
                },
            },
            {"name": "B", "locus": "other/file.py::helper"},
        ]
    }
    admitted = admit_and_validate(payload, schema, "test")
    assert admitted["strategies"][0]["locus"] == "astropy/modeling/separable.py::_separable"
    assert admitted["strategies"][1]["locus"] == "other/file.py::helper"


def test_omit_null_on_optional_non_nullable_field():
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["summary"],
    }
    payload = {"summary": "ok", "note": None}
    coerced = coerce_llm_json_from_schema(payload, schema)
    assert coerced == {"summary": "ok"}
    assert admit_and_validate(payload, schema, "test") == {"summary": "ok"}


def test_keep_null_on_nullable_optional_field():
    schema = {
        "type": "object",
        "properties": {"comment_body": {"type": ["string", "null"]}},
    }
    assert coerce_llm_json_from_schema({"comment_body": None}, schema) == {"comment_body": None}
    assert admit_and_validate({"comment_body": None}, schema, "test") == {"comment_body": None}


def test_keep_null_on_required_nullable_field():
    schema = {
        "type": "object",
        "required": ["comment_body"],
        "properties": {"comment_body": {"type": ["string", "null"]}},
    }
    assert admit_and_validate({"comment_body": None}, schema, "test") == {"comment_body": None}


def test_required_non_nullable_null_becomes_missing_required():
    schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }
    with pytest.raises(ValidationError, match="summary"):
        admit_and_validate({"summary": None}, schema, "test")


def test_omit_null_nested_optional_object_field():
    schema = {
        "type": "object",
        "properties": {
            "meta": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "hint": {"type": "string"},
                },
            }
        },
    }
    payload = {"meta": {"label": "a", "hint": None}}
    assert coerce_llm_json_from_schema(payload, schema) == {"meta": {"label": "a"}}


def test_coerce_anyof_nullable_array_peel():
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "null"},
                ]
            }
        },
    }
    assert coerce_llm_json_from_schema({"tags": '["a"]'}, schema) == {"tags": ["a"]}
    assert coerce_llm_json_from_schema({"tags": "null"}, schema) == {"tags": None}
    assert coerce_llm_json_from_schema({"tags": None}, schema) == {"tags": None}


def test_coerce_string_list_to_string_constraints():
    """GPT-style string arrays admit into string fields (SWE architect constraints)."""
    schema = {
        "type": "object",
        "properties": {"constraints": {"type": "string", "minLength": 1}},
        "required": ["constraints"],
    }
    # Single wrapped bullet
    assert admit_and_validate(
        {
            "constraints": [
                "minimal delta; no new dependencies; preserve existing __version__ behavior"
            ]
        },
        schema,
        "test",
    )["constraints"] == ("minimal delta; no new dependencies; preserve existing __version__ behavior")
    # Multiple bullets join
    assert (
        admit_and_validate(
            {"constraints": ["minimal delta", "no new deps"]},
            schema,
            "test",
        )["constraints"]
        == "minimal delta; no new deps"
    )
