"""Sync unit tests for compensation definition validation (no asyncio)."""

import pytest
from common.saga_assets import validate_compensation_definition_dict
from pydantic import ValidationError


def test_validate_compensation_definition_dict_rejects_missing_worker() -> None:
    with pytest.raises(ValidationError):
        validate_compensation_definition_dict({"with": {}})
