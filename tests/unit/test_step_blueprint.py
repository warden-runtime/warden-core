"""Unit tests for discriminated catalog step blueprints."""

from __future__ import annotations

from typing import Any

import pytest
from common.schemas.step import (
    CommitStepBlueprint,
    ReasonStepBlueprint,
    StepInputSpec,
    parse_step_blueprint,
)
from pydantic import ValidationError
from tests.factories import step_definition_body


def _reason_step(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "name": "triage",
        "worker": "demo-worker",
        "prompt": "triage.j2",
    }
    defaults.update(overrides)
    return step_definition_body(**defaults)


def _commit_step(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "name": "post-comment",
        "step_kind": "commit",
        "worker": "demo-worker",
        "tools": {"allow": [{"name": "add_issue_comment"}]},
    }
    defaults.update(overrides)
    return step_definition_body(**defaults)


def test_reason_step_requires_prompt():
    with pytest.raises(ValidationError, match="prompt"):
        parse_step_blueprint(_reason_step(prompt=None))


def test_reason_step_rejects_blank_prompt():
    with pytest.raises(ValidationError, match="reason steps require a non-empty prompt"):
        parse_step_blueprint(_reason_step(prompt="   "))


def test_reason_step_accepts_prompt_and_optional_tools():
    step = parse_step_blueprint(
        _reason_step(tools={"allow": [{"name": "list_issues"}]}, title="Triage open issues")
    )
    assert isinstance(step, ReasonStepBlueprint)
    assert step.step_kind == "reason"
    assert step.prompt == "triage.j2"
    assert step.display_title() == "Triage open issues"


def test_reason_step_display_title_falls_back_to_name():
    step = parse_step_blueprint(_reason_step())
    assert step.display_title() == "triage"


def test_commit_step_requires_exactly_one_tool():
    with pytest.raises(ValidationError, match="exactly one tool in tools.allow"):
        parse_step_blueprint(
            _commit_step(tools={"allow": [{"name": "tool_a"}, {"name": "tool_b"}]})
        )


def test_commit_step_rejects_empty_tools_allow():
    with pytest.raises(ValidationError, match="exactly one tool in tools.allow"):
        parse_step_blueprint(_commit_step(tools={"allow": []}))


def test_commit_step_requires_tools_block():
    with pytest.raises(ValidationError):
        parse_step_blueprint(_commit_step(tools=None))


def test_commit_step_rejects_tools_bind():
    with pytest.raises(ValidationError, match="tools.bind is not supported on commit steps"):
        parse_step_blueprint(
            _commit_step(
                inputs={"issue_number": {"required": True}},
                tools={"allow": [{"name": "add_issue_comment"}], "bind": ["issue_number"]},
            )
        )


def test_commit_step_rejects_reason_only_fields():
    """Discrimination: reason-only keys are forbidden extras on commit."""
    for field, value in (
        ("prompt", "nope.j2"),
        ("agent-adapter", "simple"),
        ("facts", [{"tool": "add_issue_comment", "into": "posted", "fields": {"id": "$.id"}}]),
        ("skills", {"allow": [{"name": "howto"}]}),
        ("max_turns", 10),
        ("max_step_tokens", 1000),
    ):
        with pytest.raises(ValidationError):
            parse_step_blueprint(_commit_step(**{field: value}))


def test_commit_step_accepts_single_tool():
    step = parse_step_blueprint(_commit_step())
    assert isinstance(step, CommitStepBlueprint)
    assert step.step_kind == "commit"
    assert [t.name for t in step.tools.allow] == ["add_issue_comment"]


def test_commit_round_trip_dump_omits_agent_adapter():
    step = parse_step_blueprint(_commit_step())
    body = step.model_dump(by_alias=True, exclude_none=True)
    assert "agent-adapter" not in body
    assert "prompt" not in body
    again = parse_step_blueprint(body)
    assert isinstance(again, CommitStepBlueprint)


def test_reserved_load_skill_tool_is_rejected_on_both_kinds():
    with pytest.raises(ValidationError, match="load_skill"):
        parse_step_blueprint(_reason_step(tools={"allow": [{"name": "load_skill"}]}))
    with pytest.raises(ValidationError, match="load_skill"):
        parse_step_blueprint(_commit_step(tools={"allow": [{"name": "load_skill"}]}))


def test_inputs_default_to_empty_and_declare_required_ports():
    step = parse_step_blueprint(
        _reason_step(inputs={"owner": {"required": True}, "focus": {"required": False}})
    )
    assert step.inputs["owner"].required is True
    assert step.inputs["focus"].required is False
    assert parse_step_blueprint(_reason_step()).inputs == {}


def test_input_spec_defaults_to_required():
    assert StepInputSpec().required is True


def test_inputs_reject_blank_port_names():
    with pytest.raises(ValidationError, match="inputs keys must be non-empty"):
        parse_step_blueprint(_reason_step(inputs={"  ": {"required": True}}))


def test_inputs_reject_unknown_spec_fields():
    with pytest.raises(ValidationError):
        parse_step_blueprint(_reason_step(inputs={"owner": {"mandatory": True}}))


def test_reason_tools_bind_keys_must_be_declared_inputs():
    with pytest.raises(ValidationError, match="tools.bind keys must also be declared in step"):
        parse_step_blueprint(
            _reason_step(tools={"allow": [{"name": "list_issues"}], "bind": ["owner"]})
        )

    step = parse_step_blueprint(
        _reason_step(
            inputs={"owner": {"required": True}},
            tools={"allow": [{"name": "list_issues"}], "bind": ["owner"]},
        )
    )
    assert isinstance(step, ReasonStepBlueprint)
    assert step.tools is not None
    assert step.tools.bind == ["owner"]


def test_simple_agent_adapter_rejects_tools():
    with pytest.raises(ValidationError, match="simple agent-adapter requires an empty tools.allow"):
        parse_step_blueprint(
            _reason_step(**{"agent-adapter": "simple", "tools": {"allow": [{"name": "get_me"}]}})
        )


def test_hitl_retry_fields_require_hitl():
    with pytest.raises(ValidationError, match="require hitl: true"):
        parse_step_blueprint(_reason_step(hitl_max_retries=2))

    step = parse_step_blueprint(
        _reason_step(hitl=True, hitl_max_retries=2, hitl_retry_guidance="Be careful.")
    )
    assert step.hitl_max_retries == 2


def test_identity_fields_must_be_non_empty():
    with pytest.raises(ValidationError, match="must be non-empty"):
        parse_step_blueprint(_reason_step(version="  "))


def test_step_blueprint_rejects_saga_only_fields():
    """Catalog steps have no saga-local identity (id / with / when)."""
    for field, value in (("id", "s1"), ("with", {}), ("when", {"cel": "true"})):
        with pytest.raises(ValidationError):
            parse_step_blueprint(_reason_step(**{field: value}))


def test_to_capability_dict_carries_catalog_ref_and_reason_fields():
    step = parse_step_blueprint(
        _reason_step(
            title="Triage",
            tools={"allow": [{"name": "list_issues"}]},
            output_schema="triage.json",
            timeout_seconds=300,
            max_turns=15,
        )
    )
    capability = step.to_capability_dict()

    assert capability["kind"] == "reason"
    assert capability["step_definition_name"] == "triage"
    assert capability["step_definition_version"] == "1.0.0"
    assert capability["prompt"] == "triage.j2"
    assert capability["agent-adapter"] == "react"
    assert capability["output_schema"] == "triage.json"
    assert capability["timeout_seconds"] == 300
    assert capability["max_turns"] == 15
    assert "id" not in capability
    assert "with" not in capability
    assert "when" not in capability


def test_to_capability_dict_for_commit_omits_reason_only_fields():
    capability = parse_step_blueprint(_commit_step(policy="gate.yaml")).to_capability_dict()

    assert capability["kind"] == "commit"
    assert capability["policy"] == "gate.yaml"
    assert capability["tools"]["allow"][0]["name"] == "add_issue_comment"
    assert "prompt" not in capability
    assert "agent-adapter" not in capability
    assert "max_turns" not in capability
