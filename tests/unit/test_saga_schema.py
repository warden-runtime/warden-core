"""Saga blueprint step kind validation."""

import pytest
from common.schemas.saga import (
    CommitSagaStep,
    ReasonSagaStep,
    SagaBlueprint,
    SagaStep,
    StepFactsExtractor,
    StepWhenSpec,
)
from pydantic import TypeAdapter, ValidationError

_SAGA_STEP_ADAPTER = TypeAdapter(SagaStep)


def test_saga_step_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        _SAGA_STEP_ADAPTER.validate_python(
            {
                "id": "x",
                "name": "X",
                "kind": "worker",
                "worker": "w",
                "worker_version": "1.0.0",
                "with": {},
            }
        )


def test_reason_step_requires_prompt() -> None:
    with pytest.raises(ValidationError, match="prompt"):
        ReasonSagaStep.model_validate(
            {
                "id": "r1",
                "name": "R",
                "kind": "reason",
                "worker": "w",
                "worker_version": "1.0.0",
                "with": {},
                "prompt": "   ",
            }
        )


def test_commit_step_requires_one_tool() -> None:
    with pytest.raises(ValidationError, match="exactly one tool"):
        CommitSagaStep.model_validate(
            {
                "id": "c1",
                "name": "C",
                "kind": "commit",
                "worker": "w",
                "worker_version": "1.0.0",
                "with": {},
                "tools": {"allow": []},
            }
        )


def test_blueprint_parses_reason_and_commit_steps() -> None:
    bp = SagaBlueprint.model_validate(
        {
            "kind": "saga",
            "name": "mixed",
            "version": "1",
            "description": "d",
            "steps": [
                {
                    "id": "r",
                    "name": "R",
                    "kind": "reason",
                    "worker": "w",
                    "worker_version": "1.0.0",
                    "with": {},
                    "prompt": "p.j2",
                },
                {
                    "id": "c",
                    "name": "C",
                    "kind": "commit",
                    "worker": "w",
                    "worker_version": "1.0.0",
                    "with": {},
                    "tools": {"allow": [{"name": "t1"}]},
                },
            ],
        }
    )
    assert isinstance(bp.steps[0], ReasonSagaStep)
    assert isinstance(bp.steps[1], CommitSagaStep)


def test_step_when_spec_parses_cel() -> None:
    spec = StepWhenSpec.model_validate({"cel": "input.run == true"})
    assert spec.cel == "input.run == true"


def test_step_when_spec_rejects_empty_cel() -> None:
    with pytest.raises(ValidationError, match="when.cel must be non-empty"):
        StepWhenSpec.model_validate({"cel": "   "})


def test_step_when_spec_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        StepWhenSpec.model_validate({"cel": "true", "extra": 1})


def test_reason_step_parses_when_block() -> None:
    step = ReasonSagaStep.model_validate(
        {
            "id": "r1",
            "name": "R",
            "kind": "reason",
            "worker": "w",
            "worker_version": "1.0.0",
            "with": {},
            "prompt": "p.j2",
            "when": {"cel": "input.enabled == true"},
        }
    )
    assert step.when is not None
    assert step.when.cel == "input.enabled == true"


def test_step_facts_extractor_parses_fields() -> None:
    spec = StepFactsExtractor.model_validate(
        {
            "tool": "list_issues",
            "into": "list_issues",
            "fields": {"total_count": "$.totalCount"},
        }
    )
    assert spec.tool == "list_issues"
    assert spec.fields["total_count"] == "$.totalCount"


def test_reason_step_parses_facts_block() -> None:
    step = ReasonSagaStep.model_validate(
        {
            "id": "r1",
            "name": "R",
            "kind": "reason",
            "worker": "w",
            "worker_version": "1.0.0",
            "with": {},
            "prompt": "p.j2",
            "facts": [
                {
                    "tool": "list_issues",
                    "into": "list_issues",
                    "fields": {"total_count": "$.totalCount"},
                }
            ],
        }
    )
    assert step.facts is not None
    assert len(step.facts) == 1


def test_reason_step_rejects_duplicate_facts_into() -> None:
    with pytest.raises(ValidationError, match="unique 'into'"):
        ReasonSagaStep.model_validate(
            {
                "id": "r1",
                "name": "R",
                "kind": "reason",
                "worker": "w",
                "worker_version": "1.0.0",
                "with": {},
                "prompt": "p.j2",
                "facts": [
                    {
                        "tool": "a",
                        "into": "same",
                        "fields": {"x": "$.x"},
                    },
                    {
                        "tool": "b",
                        "into": "same",
                        "fields": {"y": "$.y"},
                    },
                ],
            }
        )
