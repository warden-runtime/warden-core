"""Provider-safe MCP tool name sanitization and allowlist matching."""

from __future__ import annotations

import re

_ILLEGAL = re.compile(r"[^a-zA-Z0-9_-]+")
_MULTI_UNDERSCORE = re.compile(r"_+")
_MAX_TOOL_NAME_LEN = 64


def sanitize_mcp_tool_name(name: str) -> str:
    """Return a provider-safe tool name (``^[a-zA-Z0-9_-]{1,64}$``).

    Illegal characters (including ``.``) become ``_``; consecutive underscores
    collapse; leading/trailing underscores are stripped. Empty results become
    ``tool``. Length is capped at 64.
    """
    cleaned = _ILLEGAL.sub("_", name or "")
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip("_")
    if not cleaned:
        cleaned = "tool"
    return cleaned[:_MAX_TOOL_NAME_LEN]


def allocate_unique_sanitized_name(mcp_name: str, used: set[str]) -> str:
    """Allocate a unique sanitized alias for ``mcp_name`` within ``used``."""
    base = sanitize_mcp_tool_name(mcp_name)
    if base not in used:
        used.add(base)
        return base
    index = 2
    while True:
        suffix = f"_{index}"
        candidate = base[: _MAX_TOOL_NAME_LEN - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def resolve_unique_tool_aliases(mcp_names: list[str]) -> dict[str, str]:
    """Map sanitized alias → original MCP name for a step's tool set."""
    used: set[str] = set()
    aliases: dict[str, str] = {}
    for mcp_name in mcp_names:
        alias = allocate_unique_sanitized_name(mcp_name, used)
        aliases[alias] = mcp_name
    return aliases


def allowlist_matches(
    allow_name: str,
    mcp_name: str,
    sanitized: str | None = None,
) -> bool:
    """True when ``allow_name`` is the raw MCP id or its sanitized form (Option B)."""
    san = sanitized if sanitized is not None else sanitize_mcp_tool_name(mcp_name)
    return allow_name == mcp_name or allow_name == san


def matching_allowlist_entry(mcp_name: str, allowed_tools: list[str]) -> str | None:
    """Return the allowlist entry that matches ``mcp_name``, or None."""
    sanitized = sanitize_mcp_tool_name(mcp_name)
    for allow in allowed_tools:
        if allowlist_matches(allow, mcp_name, sanitized):
            return allow
    return None


def allowlist_entry_satisfied(allow_name: str, loaded_mcp_names: set[str]) -> bool:
    """True when some loaded MCP tool satisfies the allowlist entry."""
    if allow_name in loaded_mcp_names:
        return True
    for mcp_name in loaded_mcp_names:
        if allowlist_matches(allow_name, mcp_name):
            return True
    return False
