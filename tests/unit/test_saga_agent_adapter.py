"""Saga YAML agent-adapter validation."""

import pytest
from common.schemas.saga import DEFAULT_AGENT_ADAPTER, ReasonSagaStep
from pydantic import ValidationError


def _reason_step(**overrides):
    base = {
        "id": "step1",
        "name": "step1",
        "kind": "reason",
        "worker": "w",
        "worker_version": "1.0.0",
        "prompt": "noop.j2",
    }
    base.update(overrides)
    return ReasonSagaStep.model_validate(base)


def test_reason_step_default_agent_adapter():
    step = _reason_step()
    assert step.agent_adapter == DEFAULT_AGENT_ADAPTER


def test_reason_step_accepts_agent_adapter_alias():
    step = ReasonSagaStep.model_validate(
        {
            "id": "step1",
            "name": "step1",
            "kind": "reason",
            "worker": "w",
            "worker_version": "1.0.0",
            "prompt": "noop.j2",
            "agent-adapter": "simple",
            "tools": {"allow": []},
        }
    )
    assert step.agent_adapter == "simple"


def test_simple_rejects_non_empty_tools():
    with pytest.raises(ValidationError, match="tools.allow"):
        _reason_step(agent_adapter="simple", tools={"allow": [{"name": "echo"}]})


def test_simple_rejects_resources():
    with pytest.raises(ValidationError, match="resources.allow"):
        _reason_step(
            agent_adapter="simple",
            resources={"allow": [{"uri": "file://x"}]},
        )


def test_simple_rejects_facts():
    with pytest.raises(ValidationError, match="facts"):
        _reason_step(
            agent_adapter="simple",
            facts=[{"tool": "echo", "into": "x", "fields": {"k": "$.a"}}],
        )


def test_simple_rejects_skills():
    with pytest.raises(ValidationError, match="skills.allow"):
        _reason_step(
            agent_adapter="simple",
            skills={"allow": [{"name": "triage"}]},
        )


def test_reason_accepts_skills_allow():
    step = _reason_step(skills={"allow": [{"name": "triage"}]})
    assert step.skills is not None
    assert step.skills.allow[0].name == "triage"


def test_skill_name_load_skill_reserved():
    with pytest.raises(ValidationError, match="load_skill"):
        _reason_step(skills={"allow": [{"name": "load_skill"}]})


def test_tools_allow_rejects_load_skill():
    with pytest.raises(ValidationError, match="load_skill"):
        _reason_step(tools={"allow": [{"name": "load_skill"}]})
