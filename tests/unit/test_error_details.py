"""Unit tests for common.error_details helpers."""

from __future__ import annotations

from common.error_details import build_step_error_details, format_step_error_brief


def test_build_step_error_details_includes_code_and_message() -> None:
    details = build_step_error_details(
        code="NO_SUBMIT", message="submit missing", reason="max_turns"
    )
    assert details["code"] == "NO_SUBMIT"
    assert details["message"] == "submit missing"
    assert details["reason"] == "max_turns"


def test_format_step_error_brief_message_only_dict() -> None:
    """Fallback chain handles message-only payloads without raising."""
    brief = format_step_error_brief({"message": "Raw unexpected error string"})
    assert brief == "UNKNOWN_ERROR (Raw unexpected error string)"


def test_format_step_error_brief_prefers_last_tool_errors() -> None:
    details = {
        "code": "no_submit_call",
        "message": "no submit",
        "last_tool_errors": [{"tool": "list_issues", "preview": "failed to list issues: bad repo"}],
    }
    brief = format_step_error_brief(details)
    assert brief.startswith("no_submit_call (failed to list issues")


def test_format_step_error_brief_prefers_last_assistant_content_for_no_submit() -> None:
    details = {
        "code": "no_submit_call",
        "message": "no submit",
        "reason": "model_text_exit",
        "last_assistant_content": "I finished the work without calling _submit.",
    }
    brief = format_step_error_brief(details)
    assert brief.startswith("no_submit_call (I finished the work")


def test_format_step_error_brief_simple_lane_structured_output_failed() -> None:
    details = {
        "code": "structured_output_failed",
        "message": "Model did not return valid structured JSON for this step.",
    }
    brief = format_step_error_brief(details)
    assert "structured_output_failed" in brief
    assert "Model did not return" in brief


def test_format_step_error_brief_facts_tool_result_preview() -> None:
    details = {
        "code": "FACT_EXTRACTION_FAILED",
        "message": "tool returned error",
        "tool_result_preview": "failed to list issues: no repo",
    }
    brief = format_step_error_brief(details)
    assert "FACT_EXTRACTION_FAILED" in brief
    assert "failed to list issues" in brief


def test_format_step_error_brief_worker_config_load_failed() -> None:
    details = {
        "code": "worker_config_load_failed",
        "error": (
            "No API key found for openai (Namespace: default). "
            "Add a row to provider_secrets or set the provider's API key in the environment."
        ),
    }
    brief = format_step_error_brief(details)
    assert brief.startswith("worker_config_load_failed (No API key found for openai")


def test_format_step_error_brief_tool_result_truncated() -> None:
    details = {
        "code": "TOOL_RESULT_TRUNCATED",
        "message": "tool 'list_issues' result was truncated at the worker record limit",
        "tool_result_preview": '{"totalCount": 1, "issues": [{"body": "yy',
        "truncation_limit": 8000,
    }
    brief = format_step_error_brief(details)
    assert "TOOL_RESULT_TRUNCATED" in brief
    assert "totalCount" in brief


def test_format_step_error_brief_surfaces_mcp_source_failures() -> None:
    details = {
        "code": "MCP_UNAVAILABLE",
        "source_failures": [
            {
                "name": "github",
                "error": (
                    "Required worker environment variable(s) not set or empty: "
                    "GITHUB_PERSONAL_ACCESS_TOKEN"
                ),
            }
        ],
    }
    brief = format_step_error_brief(details, preview_len=120)
    assert brief.startswith("MCP_UNAVAILABLE")
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in brief
