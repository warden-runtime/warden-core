"""Sync unit tests for compensation definition validation (no asyncio)."""

import pytest
from common.saga_assets import validate_compensation_definition_dict
from pydantic import ValidationError


def _valid_compensation(**overrides: object) -> dict:
    base: dict = {
        "worker": "payments-worker",
        "worker_version": "1.0.0",
        "with": {},
        "tools": {"allow": [{"name": "cancel_payment"}]},
    }
    base.update(overrides)
    return base


def test_validate_compensation_definition_dict_rejects_missing_worker() -> None:
    with pytest.raises(ValidationError):
        validate_compensation_definition_dict({"with": {}, "tools": {"allow": [{"name": "undo"}]}})


def test_validate_compensation_accepts_exactly_one_tool() -> None:
    out = validate_compensation_definition_dict(_valid_compensation())
    assert out["tools"]["allow"] == [{"name": "cancel_payment"}]
    assert "max_turns" not in out


def test_validate_compensation_rejects_zero_tools() -> None:
    with pytest.raises(ValidationError, match="exactly one tool"):
        validate_compensation_definition_dict(_valid_compensation(tools={"allow": []}))


def test_validate_compensation_rejects_multi_tool() -> None:
    with pytest.raises(ValidationError, match="exactly one tool"):
        validate_compensation_definition_dict(
            _valid_compensation(tools={"allow": [{"name": "a"}, {"name": "b"}]})
        )


def test_validate_compensation_rejects_missing_tools() -> None:
    with pytest.raises(ValidationError):
        validate_compensation_definition_dict(
            {
                "worker": "payments-worker",
                "worker_version": "1.0.0",
                "with": {},
            }
        )


def test_validate_compensation_rejects_max_turns() -> None:
    with pytest.raises(ValidationError, match="max_turns"):
        validate_compensation_definition_dict(_valid_compensation(max_turns=15))


def test_validate_compensation_rejects_timeout_seconds() -> None:
    with pytest.raises(ValidationError, match="timeout_seconds"):
        validate_compensation_definition_dict(_valid_compensation(timeout_seconds=30))


def test_validate_compensation_rejects_load_skill() -> None:
    with pytest.raises(ValidationError, match="load_skill"):
        validate_compensation_definition_dict(
            _valid_compensation(tools={"allow": [{"name": "load_skill"}]})
        )
