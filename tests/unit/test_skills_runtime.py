"""Adapter/runtime tests for skills resolution and reserved load_skill."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from common.agent_adapter import ExecutionStepError
from common.skills import LOAD_SKILL_TOOL_NAME
from workers.adapters.langchain import _resolve_skills_for_step
from workers.tools import build_tools_for_worker

_TRIAGE_DOC = {
    "name": "triage",
    "description": "Classify issues.",
    "allowed_tools": ["github.get_issue"],
    "body": "Body here.",
}


def test_resolve_skills_fail_fast_missing():
    with pytest.raises(ExecutionStepError) as ei:
        _resolve_skills_for_step(
            skill_specs=[{"name": "missing"}],
            extras=[],
            skills_definition=[],
        )
    assert ei.value.error_details is not None
    assert ei.value.error_details.get("code") == "SKILL_NOT_FOUND"
    assert ei.value.error_details.get("code") != "worker_config_load_failed"


def test_resolve_skills_union_and_index():
    tools, index = _resolve_skills_for_step(
        skill_specs=[{"name": "triage"}],
        extras=[{"name": "github_get_issue", "strict_schema": {"type": "object"}}],
        skills_definition=[_TRIAGE_DOC],
    )
    assert index == [{"name": "triage", "description": "Classify issues."}]
    assert len(tools) == 1
    assert tools[0].get("strict_schema") == {"type": "object"}


@pytest.mark.asyncio
async def test_build_tools_rejects_load_skill_in_tool_specs():
    worker = SimpleNamespace(name="w", tool_sources=[])
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        with pytest.raises(ExecutionStepError) as ei:
            await build_tools_for_worker(
                worker,
                tool_specs=[{"name": LOAD_SKILL_TOOL_NAME}],
                exit_stack=stack,
            )
    assert ei.value.error_details.get("code") == "reserved_tool_name"


@pytest.mark.asyncio
async def test_build_tools_injects_load_skill_from_frozen_definition():
    worker = SimpleNamespace(name="w", tool_sources=[])
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        tools = await build_tools_for_worker(
            worker,
            tool_specs=[],
            exit_stack=stack,
            skill_specs=[{"name": "triage"}],
            context={
                "skills_definition": [
                    {
                        "name": "triage",
                        "description": "d",
                        "allowed_tools": [],
                        "body": "skill body",
                    }
                ]
            },
        )
    assert any(t.name == LOAD_SKILL_TOOL_NAME for t in tools)
    load = next(t for t in tools if t.name == LOAD_SKILL_TOOL_NAME)
    body = await load.coroutine(name="triage")
    assert "skill body" in body
