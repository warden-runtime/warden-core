"""Regression: github-demo step catalog, saga composition, and triage output schema."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from common.schemas.saga import CommitSagaStep, ReasonSagaStep, SagaAuthoringBlueprint, SagaStepRef
from common.schemas.step import parse_step_blueprint
from common.step_hydrate import hydrate_authoring_blueprint
from jsonschema import Draft7Validator

_ROOT = Path(__file__).resolve().parent.parent.parent
_SAGA_PATH = _ROOT / "config" / "saga.github-demo.yaml"
_TRIAGE_STEP_PATH = _ROOT / "config" / "step.github-triage.yaml"
_POST_COMMENT_STEP_PATH = _ROOT / "config" / "step.github-post-comment.yaml"
_SCHEMA_PATH = _ROOT / "config" / "schemas" / "github-triage-output.json"


def _load_step(path: Path):
    return parse_step_blueprint(yaml.safe_load(path.read_text(encoding="utf-8")))


def _load_saga() -> SagaAuthoringBlueprint:
    return SagaAuthoringBlueprint.model_validate(
        yaml.safe_load(_SAGA_PATH.read_text(encoding="utf-8"))
    )


def test_github_triage_step_parses_with_facts_and_declared_inputs() -> None:
    triage = _load_step(_TRIAGE_STEP_PATH)

    assert triage.name == "github-triage"
    assert triage.version == "0.1.0"
    assert triage.step_kind == "reason"
    assert triage.worker == "github-demo-worker"
    assert triage.prompt == "github-triage.j2"
    assert triage.output_schema == "github-triage-output.json"
    assert triage.inputs["owner"].required is True
    assert triage.inputs["repo"].required is True
    assert triage.inputs["focus_issue_number"].required is False
    assert triage.facts is not None
    assert len(triage.facts) == 1
    assert triage.facts[0].tool == "list_issues"
    assert triage.facts[0].into == "triage_metrics"
    assert triage.facts[0].fields["total_count"] == "$.totalCount"


def test_github_post_comment_step_is_a_governed_single_tool_commit() -> None:
    post_comment = _load_step(_POST_COMMENT_STEP_PATH)

    assert post_comment.name == "github-post-comment"
    assert post_comment.step_kind == "commit"
    assert post_comment.hitl is True
    assert post_comment.policy == "github-issue-comment.yaml"
    assert post_comment.tools is not None
    assert [t.name for t in post_comment.tools.allow] == ["add_issue_comment"]
    assert sorted(post_comment.inputs) == ["body", "issue_number", "owner", "repo"]


def test_github_demo_saga_composes_catalog_steps_with_when_on_commit_step() -> None:
    blueprint = _load_saga()
    triage, post_comment = blueprint.steps

    assert isinstance(triage, SagaStepRef)
    assert triage.id == "triage"
    assert triage.use == "github-triage"
    assert triage.version == "0.1.0"
    assert triage.when is None
    assert sorted(triage.with_spec) == ["focus_issue_number", "owner", "repo"]

    assert isinstance(post_comment, SagaStepRef)
    assert post_comment.id == "post-comment"
    assert post_comment.use == "github-post-comment"
    assert post_comment.version == "0.1.0"
    assert post_comment.when is not None
    assert post_comment.when.cel == (
        "has(steps.triage.facts.triage_metrics) && steps.triage.facts.triage_metrics.total_count > 0"
    )
    assert post_comment.with_spec["issue_number"].from_path == (
        "$.steps.triage.output.data.recommended_issue_number"
    )


def test_github_demo_saga_hydrates_against_the_shipped_step_manifests() -> None:
    catalog = {
        (step.name, step.version): step
        for step in (_load_step(_TRIAGE_STEP_PATH), _load_step(_POST_COMMENT_STEP_PATH))
    }
    hydrated = hydrate_authoring_blueprint(
        _load_saga(),
        resolve_step=lambda name, version: catalog[(name, version)],
    )

    triage, post_comment = hydrated.steps
    assert isinstance(triage, ReasonSagaStep)
    assert triage.step_definition_name == "github-triage"
    assert triage.step_definition_version == "0.1.0"
    assert triage.name == "Triage open issues"
    assert triage.facts is not None

    assert isinstance(post_comment, CommitSagaStep)
    assert post_comment.step_definition_name == "github-post-comment"
    assert post_comment.hitl is True
    assert post_comment.when is not None


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
