"""Load worker-scoped skill files from SKILLS_ROOT (shared by engine registration and workers)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from common.asset_paths import resolve_asset_path

LOAD_SKILL_TOOL_NAME = "load_skill"


class SkillLoadError(Exception):
    """Skill file missing or invalid; map to SKILL_NOT_FOUND / SKILL_INVALID at the call site."""

    def __init__(self, code: str, message: str, *, skill: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.skill = skill


@dataclass(frozen=True)
class SkillDocument:
    """Parsed skill frontmatter + markdown body."""

    name: str
    description: str
    allowed_tools: list[str]
    body: str


def resolve_skills_root(skills_root: str | None) -> str:
    """Return absolute SKILLS_ROOT; raises SkillLoadError if unset or not a directory."""
    if not skills_root or not str(skills_root).strip():
        raise SkillLoadError(
            "SKILLS_ROOT_UNSET",
            "SKILLS_ROOT must be set when a step uses skills.allow.",
        )
    root_abs = os.path.abspath(skills_root)
    if not os.path.isdir(root_abs):
        raise SkillLoadError(
            "SKILLS_ROOT_UNSET",
            f"SKILLS_ROOT is not a directory: {skills_root}",
        )
    return root_abs


def _require_safe_path_segment(
    value: str,
    *,
    label: str,
    skill: str | None = None,
    allow_separators: bool = False,
) -> str:
    stripped = (value or "").strip()
    if not stripped:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"{label} must be non-empty for skill path",
            skill=skill,
        )
    path = Path(stripped)
    if path.is_absolute() or ".." in path.parts:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Invalid {label} for skill path: {value!r}",
            skill=skill,
        )
    if not allow_separators and ("/" in stripped or "\\" in stripped):
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Invalid {label} (no path separators or '..'): {value!r}",
            skill=skill,
        )
    return stripped


def skill_relpath(worker_name: str, skill_id: str) -> str:
    """Relative path under SKILLS_ROOT for a worker-scoped skill file."""
    worker = _require_safe_path_segment(worker_name, label="worker name", allow_separators=False)
    skill = _require_safe_path_segment(
        skill_id, label="skill name", skill=skill_id, allow_separators=False
    )
    return f"{worker}/{skill}.md"


def resolved_skill_path(skills_root: str, worker_name: str, skill_id: str) -> Path:
    """Resolve skill file under SKILLS_ROOT; reject escapes and missing files."""
    root = resolve_skills_root(skills_root)
    ref = skill_relpath(worker_name, skill_id)
    try:
        return resolve_asset_path(root, ref, label="skill", root_var="SKILLS_ROOT")
    except FileNotFoundError as e:
        raise SkillLoadError(
            "SKILL_NOT_FOUND",
            str(e),
            skill=(skill_id or "").strip() or None,
        ) from e
    except ValueError as e:
        raise SkillLoadError(
            "SKILL_INVALID",
            str(e),
            skill=(skill_id or "").strip() or None,
        ) from e


def _parse_frontmatter(raw: str, *, skill_id: str) -> tuple[dict[str, Any], str]:
    text = raw if raw.startswith("---") else None
    if text is None:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill {skill_id!r} must start with YAML frontmatter (---)",
            skill=skill_id,
        )
    parts = raw.split("---", 2)
    # raw starts with --- so split yields ['', frontmatter, body...]
    if len(parts) < 3:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill {skill_id!r} frontmatter is not closed with ---",
            skill=skill_id,
        )
    meta_raw = parts[1]
    body = parts[2].lstrip("\n")
    try:
        meta = yaml.safe_load(meta_raw) or {}
    except yaml.YAMLError as e:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill {skill_id!r} frontmatter is not valid YAML: {e}",
            skill=skill_id,
        ) from e
    if not isinstance(meta, dict):
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill {skill_id!r} frontmatter must be a mapping",
            skill=skill_id,
        )
    return meta, body


def _normalize_allowed_tools(raw: Any, *, skill_id: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill {skill_id!r} allowed_tools must be a list",
            skill=skill_id,
        )
    tools: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise SkillLoadError(
                "SKILL_INVALID",
                f"Skill {skill_id!r} allowed_tools entries must be non-empty strings",
                skill=skill_id,
            )
        name = item.strip()
        if name == LOAD_SKILL_TOOL_NAME:
            raise SkillLoadError(
                "SKILL_INVALID",
                f"Skill {skill_id!r} allowed_tools must not include reserved '{LOAD_SKILL_TOOL_NAME}'",
                skill=skill_id,
            )
        tools.append(name)
    return tools


def parse_skill_document(raw: str, *, skill_id: str) -> SkillDocument:
    """Parse skill markdown with YAML frontmatter into a SkillDocument."""
    skill = (skill_id or "").strip()
    meta, body = _parse_frontmatter(raw, skill_id=skill)
    name = str(meta.get("name") or "").strip()
    if not name:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill {skill!r} frontmatter requires non-empty name",
            skill=skill,
        )
    if name != skill:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill frontmatter name {name!r} must match file stem {skill!r}",
            skill=skill,
        )
    description = str(meta.get("description") or "").strip()
    if not description:
        raise SkillLoadError(
            "SKILL_INVALID",
            f"Skill {skill!r} frontmatter requires non-empty description",
            skill=skill,
        )
    allowed_tools = _normalize_allowed_tools(meta.get("allowed_tools"), skill_id=skill)
    return SkillDocument(
        name=name,
        description=description,
        allowed_tools=allowed_tools,
        body=body,
    )


def load_skill_document(skills_root: str, worker_name: str, skill_id: str) -> SkillDocument:
    """Load and parse a skill file from disk."""
    path = resolved_skill_path(skills_root, worker_name, skill_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillLoadError(
            "SKILL_NOT_FOUND",
            f"Failed to read skill file: {path}: {e}",
            skill=(skill_id or "").strip() or None,
        ) from e
    return parse_skill_document(raw, skill_id=(skill_id or "").strip())


def load_skill_meta(skills_root: str, worker_name: str, skill_id: str) -> SkillDocument:
    """Alias for load_skill_document (meta + body); callers may ignore body."""
    return load_skill_document(skills_root, worker_name, skill_id)


def load_skill_body(skills_root: str, worker_name: str, skill_id: str) -> str:
    """Return the markdown body of a skill file."""
    return load_skill_document(skills_root, worker_name, skill_id).body


def assert_skill_files_exist(
    skills_root: str | None,
    worker_name: str,
    skill_ids: list[str],
) -> None:
    """Verify each skill file exists without requiring a full frontmatter parse."""
    if not skill_ids:
        return
    root = resolve_skills_root(skills_root)
    for skill_id in skill_ids:
        resolved_skill_path(root, worker_name, skill_id)


def validate_skill_files_at_register(
    skills_root: str | None,
    worker_name: str,
    skill_ids: list[str],
) -> list[SkillDocument]:
    """Register-time: files exist and frontmatter parses; return loaded docs."""
    if not skill_ids:
        return []
    docs: list[SkillDocument] = []
    for skill_id in skill_ids:
        docs.append(load_skill_document(skills_root or "", worker_name, skill_id))
    return docs


def validate_skills_root_if_configured() -> None:
    """Fail fast at startup when SKILLS_ROOT is set but not a readable directory."""
    from common.config import get_settings

    root = get_settings().skills_root
    if root:
        resolve_skills_root(root)
