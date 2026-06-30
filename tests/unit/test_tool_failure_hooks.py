"""Tool output failure detection via plugin registry."""

from unittest.mock import MagicMock

from common.tool_failure import (
    default_tool_output_indicates_failure,
    plain_text_tool_result_looks_like_error,
)
from workers.adapters.state_utils import tool_output_indicates_failure


def test_default_tool_output_indicates_failure_patterns():
    assert default_tool_output_indicates_failure("MCP error: connection refused") is True
    assert default_tool_output_indicates_failure("ok") is False


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

    stub_registry = MagicMock()
    stub_registry.tools = _StubTools()
    mocker.patch("common.plugins.registry.get_registry", return_value=stub_registry)

    assert tool_output_indicates_failure("registry-stub-trigger") is True
    assert tool_output_indicates_failure("anything-else") is False
