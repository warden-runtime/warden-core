"""Unit tests for common.step_hydrate (saga use: refs against catalog step definitions)."""

from __future__ import annotations

from typing import Any

import pytest
from common.schemas.saga import CommitSagaStep, ReasonSagaStep, SagaStepRef
from common.schemas.step import StepInputSpec, parse_step_blueprint
from common.step_hydrate import (
    hydrate_step_ref,
    validate_with_against_inputs,
)
from common.step_tighten import apply_tighten_overrides
from tests.factories import step_definition_body


def _reason_step_def(**overrides: Any):
    defaults: dict[str, Any] = {
        "name": "triage",
        "title": "Triage open issues",
        "inputs": {"owner": {"required": True}, "focus": {"required": False}},
        "worker": "demo-worker",
        "prompt": "triage.j2",
    }
    defaults.update(overrides)
    return parse_step_blueprint(step_definition_body(**defaults))


def _commit_step_def(**overrides: Any):
    defaults: dict[str, Any] = {
        "name": "post-comment",
        "version": "2.1.0",
        "title": "Post triage comment",
        "inputs": {"issue_number": {"required": True}},
        "step_kind": "commit",
        "worker": "demo-worker",
        "tools": {"allow": [{"name": "add_issue_comment"}]},
    }
    defaults.update(overrides)
    return parse_step_blueprint(step_definition_body(**defaults))


def _ref(**overrides: Any) -> SagaStepRef:
    base: dict[str, Any] = {
        "id": "s1",
        "use": "triage",
        "version": "1.0.0",
        "with": {"owner": {"from": "$.input.owner"}},
    }
    base.update(overrides)
    return SagaStepRef.model_validate(base)


# --- validate_with_against_inputs ---------------------------------------------------------


def test_validate_with_accepts_required_and_optional_ports():
    validate_with_against_inputs(
        step_id="s1",
        with_spec=_ref(**{"with": {"owner": {"value": "acme"}, "focus": {"value": 7}}}).with_spec,
        inputs={"owner": StepInputSpec(), "focus": StepInputSpec(required=False)},
    )


def test_validate_with_allows_omitting_optional_ports():
    validate_with_against_inputs(
        step_id="s1",
        with_spec=_ref().with_spec,
        inputs={"owner": StepInputSpec(), "focus": StepInputSpec(required=False)},
    )


def test_validate_with_rejects_unknown_keys():
    with pytest.raises(ValueError) as exc_info:
        validate_with_against_inputs(
            step_id="s1",
            with_spec=_ref(
                **{"with": {"owner": {"value": "acme"}, "nope": {"value": 1}}}
            ).with_spec,
            inputs={"owner": StepInputSpec()},
        )
    message = str(exc_info.value)
    assert "not declared inputs" in message
    assert "nope" in message


def test_validate_with_rejects_missing_required_inputs():
    with pytest.raises(ValueError) as exc_info:
        validate_with_against_inputs(
            step_id="s1",
            with_spec={},
            inputs={"owner": StepInputSpec(), "repo": StepInputSpec()},
        )
    message = str(exc_info.value)
    assert "missing required inputs" in message
    assert "owner" in message
    assert "repo" in message


# --- apply_tighten_overrides --------------------------------------------------------------


def _capability(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "reason",
        "hitl": False,
        "timeout_seconds": 600,
        "max_turns": 25,
    }
    base.update(overrides)
    return base


def test_tighten_overrides_returns_a_copy():
    capability = _capability()
    out = apply_tighten_overrides(
        step_id="s1",
        capability=capability,
        ref=_ref(hitl=True),
        step_kind="reason",
    )
    assert out["hitl"] is True
    assert capability["hitl"] is False


def test_tighten_can_turn_hitl_on():
    out = apply_tighten_overrides(
        step_id="s1",
        capability=_capability(),
        ref=_ref(hitl=True),
        step_kind="reason",
    )
    assert out["hitl"] is True


def test_tighten_cannot_clear_catalog_hitl():
    with pytest.raises(ValueError, match="cannot clear catalog hitl"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(hitl=True),
            ref=_ref(hitl=False),
            step_kind="reason",
        )


def test_hitl_retry_overrides_require_hitl_after_overrides():
    with pytest.raises(ValueError, match="hitl_max_retries requires hitl: true"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(),
            ref=_ref(hitl_max_retries=1),
            step_kind="reason",
        )

    out = apply_tighten_overrides(
        step_id="s1",
        capability=_capability(),
        ref=_ref(hitl=True, hitl_max_retries=1, hitl_retry_guidance="Check the diff."),
        step_kind="reason",
    )
    assert out["hitl_max_retries"] == 1
    assert out["hitl_retry_guidance"] == "Check the diff."


def test_hitl_max_retries_cannot_exceed_catalog_value():
    with pytest.raises(ValueError, match="hitl_max_retries cannot exceed catalog value"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(hitl=True, hitl_max_retries=2),
            ref=_ref(hitl_max_retries=5),
            step_kind="reason",
        )


def test_timeout_and_max_turns_tighten_down_only():
    out = apply_tighten_overrides(
        step_id="s1",
        capability=_capability(),
        ref=_ref(timeout_seconds=120, max_turns=5),
        step_kind="reason",
    )
    assert out["timeout_seconds"] == 120
    assert out["max_turns"] == 5

    with pytest.raises(ValueError, match="timeout_seconds cannot exceed catalog value"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(),
            ref=_ref(timeout_seconds=1200),
            step_kind="reason",
        )
    with pytest.raises(ValueError, match="max_turns cannot exceed catalog value"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(),
            ref=_ref(max_turns=99),
            step_kind="reason",
        )


def test_reason_only_overrides_are_rejected_on_commit_steps():
    for field in ("max_turns", "max_step_tokens", "max_completion_tokens"):
        with pytest.raises(ValueError, match=f"{field} override is only valid on reason steps"):
            apply_tighten_overrides(
                step_id="s1",
                capability=_capability(kind="commit"),
                ref=_ref(**{field: 5}),
                step_kind="commit",
            )


def test_token_budgets_cannot_be_introduced_when_the_catalog_leaves_them_unset():
    with pytest.raises(ValueError, match="cannot set max_step_tokens"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(),
            ref=_ref(max_step_tokens=1000),
            step_kind="reason",
        )
    with pytest.raises(ValueError, match="cannot set max_completion_tokens"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(),
            ref=_ref(max_completion_tokens=1000),
            step_kind="reason",
        )


def test_token_budgets_tighten_below_the_catalog_value():
    out = apply_tighten_overrides(
        step_id="s1",
        capability=_capability(max_step_tokens=10_000, max_completion_tokens=2_000),
        ref=_ref(max_step_tokens=5_000, max_completion_tokens=1_000),
        step_kind="reason",
    )
    assert out["max_step_tokens"] == 5_000
    assert out["max_completion_tokens"] == 1_000

    with pytest.raises(ValueError, match="max_step_tokens cannot exceed catalog value"):
        apply_tighten_overrides(
            step_id="s1",
            capability=_capability(max_step_tokens=10_000),
            ref=_ref(max_step_tokens=20_000),
            step_kind="reason",
        )


# --- hydrate_step_ref ----------------------------------------------------------------------


def test_hydrate_reason_ref_produces_a_full_saga_step():
    step = hydrate_step_ref(_ref(), _reason_step_def(tools={"allow": [{"name": "list_issues"}]}))

    assert isinstance(step, ReasonSagaStep)
    assert step.id == "s1"
    assert step.name == "Triage open issues"
    assert step.worker == "demo-worker"
    assert step.prompt == "triage.j2"
    assert step.step_definition_name == "triage"
    assert step.step_definition_version == "1.0.0"
    assert step.with_spec["owner"].from_path == "$.input.owner"
    assert step.tools is not None
    assert [t.name for t in step.tools.allow] == ["list_issues"]


def test_hydrate_uses_ref_name_override_and_carries_when():
    step = hydrate_step_ref(
        _ref(name="Triage the backlog", when={"cel": "has(input.owner)"}),
        _reason_step_def(),
    )
    assert step.name == "Triage the backlog"
    assert step.when is not None
    assert step.when.cel == "has(input.owner)"


def test_hydrate_falls_back_to_step_name_when_catalog_has_no_title():
    step = hydrate_step_ref(_ref(), _reason_step_def(title=None))
    assert step.name == "triage"


def test_hydrate_commit_ref_produces_a_commit_saga_step():
    ref = _ref(
        id="post",
        use="post-comment",
        version="2.1.0",
        **{"with": {"issue_number": {"from": "$.steps.triage.output.data.issue"}}},
    )
    step = hydrate_step_ref(ref, _commit_step_def(policy="gate.yaml", hitl=True))

    assert isinstance(step, CommitSagaStep)
    assert step.id == "post"
    assert step.name == "Post triage comment"
    assert step.policy == "gate.yaml"
    assert step.hitl is True
    assert step.step_definition_version == "2.1.0"
    assert [t.name for t in step.tools.allow] == ["add_issue_comment"]


def test_hydrate_validates_with_bindings_against_declared_inputs():
    with pytest.raises(ValueError, match="not declared inputs"):
        hydrate_step_ref(_ref(**{"with": {"nope": {"value": 1}}}), _reason_step_def())
    with pytest.raises(ValueError, match="missing required inputs"):
        hydrate_step_ref(_ref(**{"with": {}}), _reason_step_def())


def test_hydrate_applies_tighten_overrides():
    step = hydrate_step_ref(_ref(hitl=True, timeout_seconds=60), _reason_step_def())
    assert step.hitl is True
    assert step.timeout_seconds == 60

    with pytest.raises(ValueError, match="cannot clear catalog hitl"):
        hydrate_step_ref(_ref(hitl=False), _reason_step_def(hitl=True))
