"""Load prompt template files from PROMPTS_ROOT (shared by engine registration and workers)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, TemplateNotFound
from jinja2.sandbox import SandboxedEnvironment, SecurityError

# Built-in helpers that can expose object graphs; prompts only need variables + filters.
_UNSAFE_PROMPT_GLOBALS = ("cycler", "joiner", "namespace", "lipsum")


def make_prompt_environment(*, loader: BaseLoader | None = None) -> SandboxedEnvironment:
    """Jinja env for LLM prompts: sandboxed attribute access + path-safe loaders."""
    env = SandboxedEnvironment(
        loader=loader if loader is not None else BaseLoader(),
        autoescape=False,
    )  # nosemgrep: missing-autoescape-disabled
    for key in _UNSAFE_PROMPT_GLOBALS:
        env.globals.pop(key, None)
    return env


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


class _SandboxedPromptLoader(BaseLoader):
    """Jinja loader that only opens files via ``resolved_prompt_path`` (no ``..`` escapes)."""

    def __init__(self, prompts_root: str) -> None:
        self._root = resolve_prompts_root(prompts_root)

    def get_source(self, environment: Environment, template: str) -> tuple[str, str, Any]:
        try:
            path = resolved_prompt_path(self._root, template)
        except ValueError as e:
            raise TemplateNotFound(template) from e
        source = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime

        def uptodate() -> bool:
            try:
                return path.stat().st_mtime == mtime
            except OSError:
                return False

        return source, str(path), uptodate


def render_prompt_file(prompts_root: str, prompt_ref: str, context: dict[str, Any]) -> str:
    """Render a prompt file under PROMPTS_ROOT with Jinja ``{% include %}`` support."""
    ref = (prompt_ref or "").strip().lstrip("/")
    # Fail fast with the same path errors as load_prompt_content before Jinja.
    resolved_prompt_path(prompts_root, ref)
    env = make_prompt_environment(loader=_SandboxedPromptLoader(prompts_root))
    try:
        return env.get_template(ref).render(**context)
    except TemplateNotFound as e:
        raise ValueError(f"Prompt include not found or escapes PROMPTS_ROOT: {e}") from e
    except SecurityError as e:
        raise ValueError(f"Jinja render blocked unsafe construct in prompt {ref!r}: {e}") from e
    except Exception as e:
        raise ValueError(f"Jinja render failed for prompt {ref!r}: {e}") from e


def validate_prompts_root_if_configured() -> None:
    """Fail fast at startup when PROMPTS_ROOT is set but not a readable directory."""
    from common.config import get_settings

    root = get_settings().prompts_root
    if root:
        resolve_prompts_root(root)
