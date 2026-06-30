"""Unit tests for worker tool building and step-level schema validation."""

import pytest

jsonschema = pytest.importorskip("jsonschema")

from common.governance import validate_against_schema  # noqa: E402


def test_validate_against_schema_valid_passes():
    """Valid data against schema does not raise."""
    schema = {
        "type": "object",
        "properties": {"merchant_id": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["merchant_id"],
    }
    validate_against_schema({"merchant_id": "m1", "limit": 10}, schema, "test input")


def test_validate_against_schema_invalid_raises():
    """Invalid data raises ValidationError (do not swallow)."""
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number"}},
        "required": ["amount"],
    }
    with pytest.raises(jsonschema.ValidationError) as exc_info:
        validate_against_schema({"amount": "not-a-number"}, schema, "test input")
    assert "amount" in str(exc_info.value).lower() or "number" in str(exc_info.value).lower()


def test_validate_against_schema_missing_required_raises():
    """Missing required property raises ValidationError."""
    schema = {
        "type": "object",
        "properties": {"merchant_id": {"type": "string"}},
        "required": ["merchant_id"],
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_against_schema({}, schema, "test input")
