"""Unit tests for skills-first tool allowlist union."""

from __future__ import annotations

from workers.step_context import union_tool_specs_with_skill_tools


def test_union_collapses_raw_and_sanitized_names():
    effective = union_tool_specs_with_skill_tools(
        extras=[
            {
                "name": "github_get_issue",
                "strict_schema": {"type": "object"},
            }
        ],
        skill_tool_names=["github.get_issue", "list_issues"],
    )
    names = [s["name"] for s in effective]
    assert names.count("github_get_issue") == 1 or names.count("github.get_issue") == 1
    assert len(effective) == 2
    get_issue = next(s for s in effective if "get_issue" in s["name"].replace(".", "_"))
    assert get_issue.get("strict_schema") == {"type": "object"}
    assert any(s["name"] == "list_issues" for s in effective)


def test_union_extras_only_when_no_skills():
    effective = union_tool_specs_with_skill_tools(
        extras=[{"name": "ping"}],
        skill_tool_names=[],
    )
    assert effective == [{"name": "ping"}]
