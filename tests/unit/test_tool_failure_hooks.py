"""Tool output failure detection via plugin registry."""

import json
from unittest.mock import MagicMock

from common.tool_failure import (
    annotate_recoverable_tool_output,
    default_tool_output_indicates_failure,
    default_tool_output_is_recoverable,
    looks_like_apply_patch_reject,
    plain_text_tool_result_looks_like_error,
)
from workers.adapters.state_utils import tool_output_indicates_failure, tool_output_is_recoverable


def test_default_tool_output_indicates_failure_patterns():
    assert default_tool_output_indicates_failure("MCP error: connection refused") is True
    assert default_tool_output_indicates_failure("ok") is False


def test_default_tool_output_is_recoverable_edit_mismatches():
    assert (
        default_tool_output_is_recoverable(
            "Error: old_text not found in /testbed/lib/matplotlib/__init__.py"
        )
        is True
    )
    assert (
        default_tool_output_is_recoverable(
            "Error: old_text matched 3 times in /tmp/x.py; must match exactly once"
        )
        is True
    )
    assert default_tool_output_is_recoverable("Error: old_text must not be empty") is True
    assert (
        default_tool_output_is_recoverable(
            "Error: file exists (/tmp/x.py); use search_replace_sandbox or set overwrite=true"
        )
        is True
    )
    assert (
        default_tool_output_is_recoverable(
            "Error: failed to read /testbed/lib/foo.py: no such file or directory"
        )
        is True
    )
    assert default_tool_output_is_recoverable("Error: patch does not apply") is True


def test_default_tool_output_is_recoverable_keeps_transport_hard():
    assert default_tool_output_is_recoverable("MCP error: connection refused") is False
    assert default_tool_output_is_recoverable("Error: permission denied") is False
    assert default_tool_output_is_recoverable("ok wrote file") is False
    assert default_tool_output_is_recoverable("input validation error: foo") is False
    assert default_tool_output_is_recoverable("Error: failed to create Docker client: boom") is False


def test_annotate_recoverable_tool_output_adds_old_text_hint():
    raw = "Error: old_text not found in /testbed/lib/matplotlib/__init__.py"
    annotated = annotate_recoverable_tool_output(raw)
    assert annotated.startswith(raw)
    assert "read_file_sandbox" in annotated
    assert "search_replace_sandbox" in annotated


def test_annotate_apply_patch_reject_json():
    raw = json.dumps({"ok": False, "phase": "dry_run", "stdout": "patch does not apply", "stderr": ""})
    assert looks_like_apply_patch_reject(raw) is True
    annotated = annotate_recoverable_tool_output(raw)
    assert "Fall back to search_replace_sandbox" in annotated


def test_annotate_leaves_hard_errors_alone():
    raw = "MCP error: connection refused"
    assert annotate_recoverable_tool_output(raw) == raw


def test_plain_text_tool_result_looks_like_error_github_mcp_style():
    assert (
        plain_text_tool_result_looks_like_error(
            "failed to list issues: Could not resolve to a Repository"
        )
        is True
    )


def test_plain_text_tool_result_looks_like_error_ignores_json_payload():
    assert plain_text_tool_result_looks_like_error('{"status": "failed to locate server"}') is False


def test_plain_text_tool_result_looks_like_error_informational_prose():
    assert (
        plain_text_tool_result_looks_like_error("could not find matching entries in database")
        is True
    )


def test_state_utils_uses_runtime_registry_registration(mocker):
    """Mutating the registry changes state_utils evaluation (not a static default)."""

    class _StubTools:
        def tool_output_indicates_failure(self, output: str) -> bool:
            return output == "registry-stub-trigger"

        def tool_output_is_recoverable(self, output: str) -> bool:
            return output == "registry-stub-recoverable"

    stub_registry = MagicMock()
    stub_registry.tools = _StubTools()
    mocker.patch("common.plugins.registry.get_registry", return_value=stub_registry)

    assert tool_output_indicates_failure("registry-stub-trigger") is True
    assert tool_output_indicates_failure("anything-else") is False
    assert tool_output_is_recoverable("registry-stub-recoverable") is True
    assert tool_output_is_recoverable("registry-stub-trigger") is False
