"""Unit tests for common.utils schema helpers."""

import pytest
from common.utils import assert_output_schema_bind_supported, create_pydantic_model_from_schema
from pydantic import ValidationError


def test_create_pydantic_model_from_schema_integer_array() -> None:
    model = create_pydantic_model_from_schema(
        {
            "properties": {
                "scores": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["scores"],
        },
        "ScoreOutput",
    )
    parsed = model(scores=[1, 2, 3])
    assert parsed.scores == [1, 2, 3]


def test_create_pydantic_model_from_schema_rejects_extra_keys_when_additional_properties_false() -> (
    None
):
    model = create_pydantic_model_from_schema(
        {
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "NamedOutput",
    )
    with pytest.raises(ValidationError):
        model(name="ok", unexpected=True)


def test_create_pydantic_model_from_schema_allows_extra_keys_by_default() -> None:
    model = create_pydantic_model_from_schema(
        {"properties": {"name": {"type": "string"}}, "required": ["name"]},
        "NamedOutputLoose",
    )
    parsed = model(name="ok", unexpected=True)
    assert parsed.name == "ok"
    assert parsed.model_extra == {"unexpected": True}


def test_create_pydantic_model_from_schema_allows_extra_keys_when_additional_properties_true() -> (
    None
):
    model = create_pydantic_model_from_schema(
        {
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": True,
        },
        "NamedOutputAllow",
    )
    parsed = model(name="ok", unexpected="x")
    assert parsed.model_extra == {"unexpected": "x"}


def test_create_pydantic_model_from_schema_number_array() -> None:
    model = create_pydantic_model_from_schema(
        {
            "properties": {
                "weights": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["weights"],
        },
        "WeightOutput",
    )
    parsed = model(weights=[0.1, 0.9])
    assert parsed.weights == [0.1, 0.9]


def test_create_pydantic_model_from_schema_nested_object() -> None:
    model = create_pydantic_model_from_schema(
        {
            "type": "object",
            "required": ["activity"],
            "additionalProperties": False,
            "properties": {
                "activity": {
                    "type": "object",
                    "required": ["title", "minutes"],
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "minutes": {"type": "integer"},
                    },
                }
            },
        },
        "NestedOutput",
    )
    parsed = model(activity={"title": "run", "minutes": 30})
    assert parsed.activity.title == "run"
    assert parsed.activity.minutes == 30
    with pytest.raises(ValidationError):
        model(activity={"title": "run", "minutes": 30, "extra": True})


def test_create_pydantic_model_from_schema_array_of_objects() -> None:
    model = create_pydantic_model_from_schema(
        {
            "type": "object",
            "required": ["events"],
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {"id": {"type": "string"}},
                    },
                }
            },
        },
        "EventsOutput",
    )
    parsed = model(events=[{"id": "a"}, {"id": "b"}])
    assert [e.id for e in parsed.events] == ["a", "b"]


def test_create_pydantic_model_from_schema_nullable_type_list() -> None:
    model = create_pydantic_model_from_schema(
        {
            "type": "object",
            "required": ["issue_number", "comment_body"],
            "properties": {
                "issue_number": {"type": ["integer", "null"]},
                "comment_body": {"type": ["string", "null"]},
            },
        },
        "NullableOutput",
    )
    parsed = model(issue_number=None, comment_body=None)
    assert parsed.issue_number is None
    assert parsed.comment_body is None
    parsed2 = model(issue_number=7, comment_body="hi")
    assert parsed2.issue_number == 7
    assert parsed2.comment_body == "hi"


def test_assert_output_schema_bind_supported_allows_github_triage_shape() -> None:
    assert_output_schema_bind_supported(
        {
            "type": "object",
            "required": ["summary", "recommended_issue_number", "comment_body"],
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "recommended_issue_number": {"type": ["integer", "null"], "minimum": 1},
                "comment_body": {"type": ["string", "null"], "minLength": 1},
            },
            "additionalProperties": False,
        }
    )


@pytest.mark.parametrize(
    "keyword",
    ["if", "then", "else", "allOf", "anyOf", "oneOf", "$ref", "$defs", "definitions"],
)
def test_assert_output_schema_bind_supported_rejects_composition(keyword: str) -> None:
    schema = {
        "type": "object",
        "properties": {
            "event": {
                "type": "object",
                "properties": {"op": {"type": "string"}},
                keyword: [{"type": "object"}] if keyword != "$ref" else "#/definitions/x",
            }
        },
    }
    with pytest.raises(ValueError, match="unsupported keyword"):
        assert_output_schema_bind_supported(schema)
