"""Unit tests for MCP tool-name sanitization and Option B allowlist matching."""

from workers.tool_names import (
    allocate_unique_sanitized_name,
    allowlist_entry_satisfied,
    allowlist_matches,
    matching_allowlist_entry,
    resolve_unique_tool_aliases,
    sanitize_mcp_tool_name,
)


def test_sanitize_dots_to_underscores():
    assert sanitize_mcp_tool_name("calendar.list_events") == "calendar_list_events"


def test_sanitize_identity_for_valid_names():
    assert sanitize_mcp_tool_name("list_issues") == "list_issues"
    assert sanitize_mcp_tool_name("add-issue-comment") == "add-issue-comment"


def test_sanitize_length_cap():
    long_name = "a" * 80
    assert len(sanitize_mcp_tool_name(long_name)) == 64


def test_sanitize_collapses_illegal_runs():
    assert sanitize_mcp_tool_name("foo..bar!!baz") == "foo_bar_baz"


def test_resolve_unique_aliases_collision_suffixes():
    aliases = resolve_unique_tool_aliases(["foo.bar", "foo_bar"])
    assert set(aliases.values()) == {"foo.bar", "foo_bar"}
    assert len(aliases) == 2
    assert "foo_bar" in aliases
    assert "foo_bar_2" in aliases


def test_allocate_unique_sanitized_name_deterministic():
    used: set[str] = set()
    first = allocate_unique_sanitized_name("calendar.list_events", used)
    second = allocate_unique_sanitized_name("calendar_list_events", used)
    assert first == "calendar_list_events"
    assert second == "calendar_list_events_2"


def test_allowlist_matches_raw_or_sanitized():
    mcp = "calendar.list_events"
    san = "calendar_list_events"
    assert allowlist_matches(mcp, mcp, san)
    assert allowlist_matches(san, mcp, san)
    assert not allowlist_matches("other_tool", mcp, san)


def test_matching_allowlist_entry_option_b():
    allowed = ["calendar_list_events", "list_issues"]
    assert matching_allowlist_entry("calendar.list_events", allowed) == "calendar_list_events"
    assert matching_allowlist_entry("list_issues", allowed) == "list_issues"
    assert matching_allowlist_entry("missing.tool", allowed) is None


def test_allowlist_entry_satisfied_against_loaded_mcp_names():
    loaded = {"calendar.list_events"}
    assert allowlist_entry_satisfied("calendar.list_events", loaded)
    assert allowlist_entry_satisfied("calendar_list_events", loaded)
    assert not allowlist_entry_satisfied("other", loaded)
