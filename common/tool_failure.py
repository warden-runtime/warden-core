"""Default tool-output failure heuristics (OSS; override via ToolLifecycleHooks)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from common.utils import format_exception_chain

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
    "is a directory",
    "not a directory",
    "patch does not apply",
    "hunk failed",
    "corrupt patch",
    "malformed patch",
)

# Substrings that indicate infrastructure collapse when seen on exception messages.
_INFRA_MESSAGE_MARKERS: tuple[str, ...] = (
    "no such container",
    "failed to create docker client",
    "connection refused",
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
_HINT_IS_DIRECTORY = (
    "[Hint: Path is a directory, not a file. Use list_dir_sandbox to inspect contents.]"
)
_HINT_NOT_DIRECTORY = (
    "[Hint: Path component is a file, not a directory. Check parent paths with list_dir_sandbox.]"
)
_HINT_VALIDATION = (
    "[Hint: Invalid tool parameters. Inspect the tool schema and fix arguments before retrying.]"
)
_HINT_APPLY_PATCH = (
    "[Hint: The unified diff did not apply. Fall back to search_replace_sandbox with a "
    "small exact hunk, or regenerate the patch from a fresh ranged read.]"
)
_HINT_GENERIC = (
    "[Hint: Fix the tool arguments from this error and retry; prefer a ranged re-read "
    "before editing.]"
)


def _is_pydantic_tool_validation_failure(lowered: str) -> bool:
    """Match Pydantic v1/v2 validation messages (singular or plural), not MCP hard errors."""
    if "input validation error" in lowered:
        return False
    return "validation error" in lowered


def _iter_exception_leaves(exc: BaseException) -> list[BaseException]:
    """Flatten ExceptionGroup, __cause__, and __context__ into leaf exceptions."""
    leaves: list[BaseException] = []
    seen: set[int] = set()

    def walk(current: BaseException) -> None:
        oid = id(current)
        if oid in seen:
            return
        seen.add(oid)
        if isinstance(current, BaseExceptionGroup):
            for sub in current.exceptions:
                walk(sub)
            return
        leaves.append(current)
        if current.__cause__ is not None:
            walk(current.__cause__)
        if current.__context__ is not None and current.__context__ is not current.__cause__:
            walk(current.__context__)

    walk(exc)
    return leaves


def _leaf_exception_is_infrastructure(exc: BaseException) -> bool:
    from common.agent_adapter import ExecutionStepError

    if isinstance(exc, ExecutionStepError):
        return True
    try:
        from mcp.shared.exceptions import MCPError

        if isinstance(exc, MCPError):
            return True
    except ImportError:
        pass
    if isinstance(
        exc,
        (
            ConnectionError,
            TimeoutError,
            BrokenPipeError,
            ConnectionResetError,
            ProcessLookupError,
        ),
    ):
        return True
    if type(exc).__name__ == "ClosedResourceError":
        return True
    lowered = str(exc).lower()
    return any(marker in lowered for marker in _INFRA_MESSAGE_MARKERS)


def tool_invoke_exception_is_infrastructure(exc: BaseException) -> bool:
    """True when any leaf in the exception tree is an infrastructure / transport failure."""
    return any(_leaf_exception_is_infrastructure(leaf) for leaf in _iter_exception_leaves(exc))


def format_tool_invoke_exception(exc: BaseException) -> str:
    """Normalize a caught tool invoke exception to a tool-role ``Error:`` payload."""
    return f"Error: {format_exception_chain(exc)}"


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


def _is_os_tool_mistake_failure(lowered: str) -> bool:
    """Filesystem / arg mistakes surfaced as exception type names in Error: payloads."""
    return any(
        token in lowered
        for token in (
            "isadirectoryerror",
            "notadirectoryerror",
            "filenotfounderror",
            "is a directory",
            "not a directory",
        )
    )


def default_tool_output_is_recoverable(output: str) -> bool:
    """True when a failure string is a recoverable mismatch to feed back to the LLM.

    Recoverable failures are a subset of :func:`default_tool_output_indicates_failure`.
    In submit-mode ReAct they should become tool-role messages instead of ``TOOL_OUTPUT_ERROR``.
    """
    if not default_tool_output_indicates_failure(output):
        return False
    lowered = output.strip().lower()
    if _is_pydantic_tool_validation_failure(lowered):
        return True
    if _is_os_tool_mistake_failure(lowered):
        return True
    return any(marker in lowered for marker in _RECOVERABLE_FAILURE_MARKERS)


def _patch_apply_hint_markers(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in ("patch does not apply", "hunk failed", "corrupt patch", "malformed patch")
    )


_RECOVERABLE_HINT_RULES: tuple[tuple[Callable[[str], bool], str], ...] = (
    (lambda lowered: "old_text" in lowered or "must match exactly once" in lowered, _HINT_OLD_TEXT),
    (lambda lowered: "file exists (" in lowered, _HINT_FILE_EXISTS),
    (lambda lowered: "no such file" in lowered, _HINT_MISSING_PATH),
    (
        lambda lowered: "isadirectoryerror" in lowered or "is a directory" in lowered,
        _HINT_IS_DIRECTORY,
    ),
    (
        lambda lowered: "notadirectoryerror" in lowered or "not a directory" in lowered,
        _HINT_NOT_DIRECTORY,
    ),
    (_is_pydantic_tool_validation_failure, _HINT_VALIDATION),
    (_patch_apply_hint_markers, _HINT_APPLY_PATCH),
)


def _recoverable_hint_for_lowered(lowered: str) -> str:
    """Map a lowered recoverable failure string to an operational hint."""
    for matches, hint in _RECOVERABLE_HINT_RULES:
        if matches(lowered):
            return hint
    return _HINT_GENERIC


def recovery_hint_for_tool_output(output: str) -> str | None:
    """One-line operational hint to append when feeding a recoverable tool error to the LLM."""
    if not output or not isinstance(output, str):
        return None
    if looks_like_apply_patch_reject(output):
        return _HINT_APPLY_PATCH
    if not default_tool_output_is_recoverable(output):
        return None
    return _recoverable_hint_for_lowered(output.strip().lower())


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
