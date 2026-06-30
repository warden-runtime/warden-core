"""Regression: github-demo saga manifest and triage output schema."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from common.schemas.saga import CommitSagaStep, ReasonSagaStep, SagaBlueprint
from jsonschema import Draft7Validator

_ROOT = Path(__file__).resolve().parent.parent.parent
_SAGA_PATH = _ROOT / "config" / "saga.github-demo.yaml"
_SCHEMA_PATH = _ROOT / "config" / "schemas" / "github-triage-output.json"


def test_github_demo_saga_parses_with_facts_and_when_on_commit_step() -> None:
    data = yaml.safe_load(_SAGA_PATH.read_text(encoding="utf-8"))
    blueprint = SagaBlueprint.model_validate(data)
    triage = blueprint.steps[0]
    post_comment = blueprint.steps[1]
    assert isinstance(triage, ReasonSagaStep)
    assert triage.id == "triage"
    assert triage.facts is not None
    assert len(triage.facts) == 1
    assert triage.facts[0].tool == "list_issues"
    assert triage.facts[0].into == "triage_metrics"
    assert triage.facts[0].fields["total_count"] == "$.totalCount"
    assert isinstance(post_comment, CommitSagaStep)
    assert post_comment.id == "post-comment"
    assert post_comment.when is not None
    assert post_comment.when.cel == (
        "has(steps.triage.facts.triage_metrics) && steps.triage.facts.triage_metrics.total_count > 0"
    )


def test_github_demo_triage_output_schema_flat_shape_with_nulls_for_empty_repo() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)
    assert validator.is_valid(
        {
            "summary": "No open issues.",
            "recommended_issue_number": None,
            "comment_body": None,
        }
    )
    assert not validator.is_valid({"summary": "No open issues."})
    assert not validator.is_valid(
        {
            "summary": "Issue without comment body key",
            "recommended_issue_number": 1,
        }
    )
    assert not validator.is_valid(
        {
            "summary": "Bad issue number",
            "recommended_issue_number": 0,
            "comment_body": None,
        }
    )
    assert validator.is_valid(
        {
            "summary": "Issue #1 needs attention.",
            "recommended_issue_number": 1,
            "comment_body": "## Warden triage\n\nDetails.",
        }
    )
