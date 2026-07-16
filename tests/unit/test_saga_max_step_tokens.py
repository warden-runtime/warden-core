"""Saga YAML max_step_tokens validation."""

import pytest
from common.schemas.saga import CommitSagaStep, ReasonSagaStep
from pydantic import ValidationError


def test_reason_step_default_max_step_tokens_is_none():
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
    assert step.max_step_tokens is None


def test_reason_step_accepts_max_step_tokens():
    step = ReasonSagaStep.model_validate(
        {
            "id": "s1",
            "kind": "reason",
            "name": "Step",
            "worker": "w",
            "worker_version": "1.0.0",
            "with": {},
            "prompt": "p.j2",
            "max_step_tokens": 50000,
        }
    )
    assert step.max_step_tokens == 50000


@pytest.mark.parametrize("bad", [0, -1])
def test_reason_step_rejects_non_positive_max_step_tokens(bad: int):
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
                "max_step_tokens": bad,
            }
        )


def test_commit_step_rejects_max_step_tokens():
    with pytest.raises(ValidationError):
        CommitSagaStep.model_validate(
            {
                "id": "c1",
                "kind": "commit",
                "name": "Commit",
                "worker": "w",
                "worker_version": "1.0.0",
                "with": {},
                "max_step_tokens": 1000,
                "tools": {"allow": [{"name": "do_thing"}]},
            }
        )
