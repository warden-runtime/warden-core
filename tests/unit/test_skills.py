"""Unit tests for common.skills loader."""

from __future__ import annotations

import pytest
from common.skills import (
    SkillLoadError,
    load_skill_document,
    parse_skill_document,
    skill_relpath,
)


def test_parse_skill_document_ok():
    raw = """---
name: triage
description: Use when classifying issues.
allowed_tools:
  - get_issue
  - list_issues
---
# Body
Do the thing.
"""
    doc = parse_skill_document(raw, skill_id="triage")
    assert doc.name == "triage"
    assert doc.description.startswith("Use when")
    assert doc.allowed_tools == ["get_issue", "list_issues"]
    assert "Do the thing" in doc.body


def test_parse_skill_name_mismatch():
    raw = """---
name: other
description: x
---
body
"""
    with pytest.raises(SkillLoadError) as ei:
        parse_skill_document(raw, skill_id="triage")
    assert ei.value.code == "SKILL_INVALID"


def test_parse_skill_rejects_load_skill_in_allowed_tools():
    raw = """---
name: triage
description: x
allowed_tools:
  - load_skill
---
body
"""
    with pytest.raises(SkillLoadError) as ei:
        parse_skill_document(raw, skill_id="triage")
    assert ei.value.code == "SKILL_INVALID"


def test_load_skill_document_from_disk(tmp_path, monkeypatch):
    worker = tmp_path / "demo-worker"
    worker.mkdir()
    (worker / "triage.md").write_text(
        """---
name: triage
description: Demo skill.
allowed_tools:
  - ping
---
Hello skill.
""",
        encoding="utf-8",
    )
    doc = load_skill_document(str(tmp_path), "demo-worker", "triage")
    assert doc.body.strip() == "Hello skill."
    assert doc.allowed_tools == ["ping"]


def test_load_skill_not_found(tmp_path):
    with pytest.raises(SkillLoadError) as ei:
        load_skill_document(str(tmp_path), "demo-worker", "missing")
    assert ei.value.code == "SKILL_NOT_FOUND"


def test_skill_relpath_rejects_traversal():
    with pytest.raises(SkillLoadError):
        skill_relpath("w", "../x")
    with pytest.raises(SkillLoadError):
        skill_relpath("../w", "x")
