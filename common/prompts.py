"""Load prompt template files from PROMPTS_ROOT (shared by engine registration and workers)."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_prompts_root(prompts_root: str | None) -> str:
    """Return absolute PROMPTS_ROOT; raises ValueError if unset or not a directory."""
    if not prompts_root:
        raise ValueError("PROMPTS_ROOT must be set when using file-based prompts.")
    root_abs = os.path.abspath(prompts_root)
    if not os.path.isdir(root_abs):
        raise ValueError(f"PROMPTS_ROOT is not a directory: {prompts_root}")
    return root_abs


def resolved_prompt_path(prompts_root: str, prompt_ref: str) -> Path:
    """Resolve ``prompt_ref`` under ``prompts_root``; reject escapes and missing files."""
    root_abs = resolve_prompts_root(prompts_root)
    ref = (prompt_ref or "").strip().lstrip("/")
    if not ref:
        raise ValueError("prompt must be a non-empty path.")
    if Path(ref).is_absolute() or ".." in Path(ref).parts:
        raise ValueError(f"Invalid prompt (no absolute paths or '..'): {prompt_ref!r}")
    base = Path(root_abs).resolve()
    candidate = (base / ref).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise ValueError(f"Prompt path escapes PROMPTS_ROOT: {prompt_ref}") from e
    if not candidate.is_file():
        raise ValueError(f"Prompt file not found: {candidate}")
    return candidate


def load_prompt_content(prompts_root: str, prompt_ref: str) -> str:
    """Load prompt template content from a file under PROMPTS_ROOT."""
    return resolved_prompt_path(prompts_root, prompt_ref).read_text(encoding="utf-8")


def assert_prompt_file_exists(prompts_root: str, prompt_ref: str) -> None:
    """Verify the prompt file exists without reading its body."""
    resolved_prompt_path(prompts_root, prompt_ref)


def validate_prompts_root_if_configured() -> None:
    """Fail fast at startup when PROMPTS_ROOT is set but not a readable directory."""
    from common.config import get_settings

    root = get_settings().prompts_root
    if root:
        resolve_prompts_root(root)
