"""Unit tests for common.utils schema helpers."""

import pytest
from common.utils import create_pydantic_model_from_schema
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


def test_create_pydantic_model_from_schema_rejects_extra_keys() -> None:
    model = create_pydantic_model_from_schema(
        {"properties": {"name": {"type": "string"}}, "required": ["name"]},
        "NamedOutput",
    )
    with pytest.raises(ValidationError):
        model(name="ok", unexpected=True)


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
