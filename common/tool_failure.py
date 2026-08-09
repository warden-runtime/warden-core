"""Default tool-output failure heuristics (OSS; override via ToolLifecycleHooks)."""

from __future__ import annotations

import json
from typing import Any

# Deterministic edit/tool mismatches the model can fix on the next ReAct turn.
# Keep this narrow — transport/MCP/"invalid arguments" stay hard step failures.
_RECOVERABLE_FAILURE_MARKERS: tuple[str, ...] = (
    "old_text not found",
    "old_text matched",
    "must match exactly once",
    "old_text must not be empty",
    "file exists (",  # write_file without overwrite; use search_replace
    "no such file or directory",
    "no such file",
    "patch does not apply",
    "hunk failed",
    "corrupt patch",
    "malformed patch",
)

_HINT_OLD_TEXT = (
    "[Hint: Use ranged read_file_sandbox on the target lines and copy the exact code "
    "(including comments and indentation) into old_text — strip any N| line prefixes — "
    "before retrying search_replace_sandbox.]"
)
_HINT_FILE_EXISTS = (
    "[Hint: That path already exists — use search_replace_sandbox "
    "(or set overwrite=true on write) instead of creating a new file.]"
)
_HINT_MISSING_PATH = (
    "[Hint: Check the path with path_exists_sandbox / glob_sandbox, then retry with "
    "the corrected absolute path.]"
)
_HINT_APPLY_PATCH = (
    "[Hint: The unified diff did not apply. Fall back to search_replace_sandbox with a "
    "small exact hunk, or regenerate the patch from a fresh ranged read.]"
)
_HINT_GENERIC = (
    "[Hint: Fix the tool arguments from this error and retry; prefer a ranged re-read "
    "before editing.]"
)


def default_tool_output_indicates_failure(output: str) -> bool:
    """Return True when tool return text matches built-in failure patterns."""
    if not output or not isinstance(output, str):
        return False
    lowered = output.strip().lower()
    return (
        lowered.startswith("mcp error")
        or lowered.startswith("error:")
        or "input validation error" in lowered
        or "invalid arguments for tool" in lowered
    )


def looks_like_apply_patch_reject(output: str) -> bool:
    """True for apply_patch_sandbox JSON rejects (``{"ok": false, "phase": ...}``).

    Those payloads do not match :func:`default_tool_output_indicates_failure`, so they
    already stay in the transcript; we still treat them as recoverable for hints.
    """
    if not output or not isinstance(output, str):
        return False
    stripped = output.strip()
    if not stripped.startswith("{"):
        return False
    try:
        data: Any = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, dict) or data.get("ok") is not False:
        return False
    return data.get("phase") in {"dry_run", "apply"}


def default_tool_output_is_recoverable(output: str) -> bool:
    """True when a failure string is a recoverable mismatch to feed back to the LLM.

    Recoverable failures are a subset of :func:`default_tool_output_indicates_failure`.
    In submit-mode ReAct they should become tool-role messages instead of ``TOOL_OUTPUT_ERROR``.
    """
    if not default_tool_output_indicates_failure(output):
        return False
    lowered = output.strip().lower()
    return any(marker in lowered for marker in _RECOVERABLE_FAILURE_MARKERS)


def recovery_hint_for_tool_output(output: str) -> str | None:
    """One-line operational hint to append when feeding a recoverable tool error to the LLM."""
    if not output or not isinstance(output, str):
        return None
    if looks_like_apply_patch_reject(output):
        return _HINT_APPLY_PATCH
    if not default_tool_output_is_recoverable(output):
        return None
    lowered = output.strip().lower()
    if "old_text" in lowered or "must match exactly once" in lowered:
        return _HINT_OLD_TEXT
    if "file exists (" in lowered:
        return _HINT_FILE_EXISTS
    if "no such file" in lowered:
        return _HINT_MISSING_PATH
    if any(
        marker in lowered
        for marker in ("patch does not apply", "hunk failed", "corrupt patch", "malformed patch")
    ):
        return _HINT_APPLY_PATCH
    return _HINT_GENERIC


def annotate_recoverable_tool_output(output: str) -> str:
    """Append a recovery hint when the tool output is a recoverable mismatch / patch reject."""
    hint = recovery_hint_for_tool_output(output)
    if not hint:
        return output
    return f"{output.rstrip()}\n{hint}"


def plain_text_tool_result_looks_like_error(raw: str) -> bool:
    """True when non-JSON tool text likely indicates a transport/API failure (facts path only)."""
    if not raw or not isinstance(raw, str):
        return False
    stripped = raw.strip()
    if not stripped or stripped[0] in "{[":
        return False
    lowered = stripped.lower()
    return (
        lowered.startswith("failed to ")
        or lowered.startswith("could not ")
        or default_tool_output_indicates_failure(stripped)
    )
