"""Saga YAML max_turns validation."""

import pytest
from common.schemas.saga import DEFAULT_MAX_TURNS, ReasonSagaStep
from pydantic import ValidationError


def test_reason_step_default_max_turns():
    step = ReasonSagaStep.model_validate(
        {
            "id": "s1",
            "kind": "reason",
            "name": "Step",
            "worker": "w",
            "worker_version": "1.0.0",
            "with": {},
            "prompt": "p.j2",
        }
    )
    assert step.max_turns == DEFAULT_MAX_TURNS


def test_reason_step_accepts_custom_max_turns():
    step = ReasonSagaStep.model_validate(
        {
            "id": "s1",
            "kind": "reason",
            "name": "Step",
            "worker": "w",
            "worker_version": "1.0.0",
            "with": {},
            "prompt": "p.j2",
            "max_turns": 25,
        }
    )
    assert step.max_turns == 25


@pytest.mark.parametrize("bad", [0, 201])
def test_reason_step_rejects_out_of_range_max_turns(bad: int):
    with pytest.raises(ValidationError):
        ReasonSagaStep.model_validate(
            {
                "id": "s1",
                "kind": "reason",
                "name": "Step",
                "worker": "w",
                "worker_version": "1.0.0",
                "with": {},
                "prompt": "p.j2",
                "max_turns": bad,
            }
        )
