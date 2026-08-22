"""Adapter/runtime tests for skills resolution and reserved load_skill."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from common.agent_adapter import ExecutionStepError
from common.config import get_settings
from common.skills import LOAD_SKILL_TOOL_NAME
from workers.adapters.langchain import _resolve_skills_for_step
from workers.tools import build_tools_for_worker


def test_resolve_skills_fail_fast_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    with pytest.raises(ExecutionStepError) as ei:
        _resolve_skills_for_step(
            worker_name="demo",
            skill_specs=[{"name": "missing"}],
            extras=[],
        )
    assert ei.value.error_details is not None
    assert ei.value.error_details.get("code") == "SKILL_NOT_FOUND"
    assert ei.value.error_details.get("code") != "worker_config_load_failed"


def test_resolve_skills_union_and_index(tmp_path, monkeypatch):
    worker = tmp_path / "demo"
    worker.mkdir()
    (worker / "triage.md").write_text(
        """---
name: triage
description: Classify issues.
allowed_tools:
  - github.get_issue
---
Body here.
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    tools, index = _resolve_skills_for_step(
        worker_name="demo",
        skill_specs=[{"name": "triage"}],
        extras=[{"name": "github_get_issue", "strict_schema": {"type": "object"}}],
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
async def test_build_tools_injects_load_skill(tmp_path, monkeypatch):
    worker_dir = tmp_path / "w"
    worker_dir.mkdir()
    (worker_dir / "triage.md").write_text(
        """---
name: triage
description: d
allowed_tools: []
---
skill body
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SKILLS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    worker = SimpleNamespace(name="w", tool_sources=[])
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        tools = await build_tools_for_worker(
            worker,
            tool_specs=[],
            exit_stack=stack,
            skill_specs=[{"name": "triage"}],
        )
    assert any(t.name == LOAD_SKILL_TOOL_NAME for t in tools)
    load = next(t for t in tools if t.name == LOAD_SKILL_TOOL_NAME)
    body = await load.coroutine(name="triage")
    assert "skill body" in body
