"""Unit tests for common.step_facts extraction."""

from __future__ import annotations

import json

import pytest
from common.schemas.saga import StepFactsExtractor
from common.step_facts import (
    FACT_EXTRACTION_FAILED,
    TOOL_RESULT_TRUNCATED,
    StepFactsExtractionError,
    extract_step_facts,
    validate_facts_extractors,
)
from common.tool_results import DEFAULT_TOOL_RESULT_RECORD_LIMIT

_LIST_ISSUES_SPEC = StepFactsExtractor(
    tool="list_issues",
    into="list_issues",
    fields={"total_count": "$.totalCount"},
)


def test_extract_step_facts_happy_path() -> None:
    tool_results = [
        {
            "tool": "list_issues",
            "result": '{"issues":[],"totalCount":0,"pageInfo":{"hasNextPage":false}}',
        }
    ]
    facts = extract_step_facts(tool_results, [_LIST_ISSUES_SPEC])
    assert facts == {"list_issues": {"total_count": 0}}


def test_extract_step_facts_omits_into_when_tool_not_called() -> None:
    facts = extract_step_facts([], [_LIST_ISSUES_SPEC])
    assert facts == {}


def test_extract_step_facts_last_call_wins() -> None:
    tool_results = [
        {"tool": "list_issues", "result": '{"totalCount": 1}'},
        {"tool": "list_issues", "result": '{"totalCount": 3}'},
    ]
    facts = extract_step_facts(tool_results, [_LIST_ISSUES_SPEC])
    assert facts["list_issues"]["total_count"] == 3


def test_extract_step_facts_fails_on_missing_jsonpath() -> None:
    tool_results = [{"tool": "list_issues", "result": '{"issues":[]}'}]
    with pytest.raises(StepFactsExtractionError) as exc_info:
        extract_step_facts(tool_results, [_LIST_ISSUES_SPEC])
    assert exc_info.value.code == FACT_EXTRACTION_FAILED
    assert exc_info.value.field == "total_count"


def test_extract_step_facts_fails_on_invalid_json() -> None:
    tool_results = [{"tool": "list_issues", "result": "not-json"}]
    with pytest.raises(StepFactsExtractionError, match="not valid JSON"):
        extract_step_facts(tool_results, [_LIST_ISSUES_SPEC])


def test_extract_step_facts_surfaces_plain_text_tool_error() -> None:
    tool_results = [
        {
            "tool": "list_issues",
            "result": "failed to list issues: Could not resolve to a Repository",
        }
    ]
    with pytest.raises(StepFactsExtractionError) as exc_info:
        extract_step_facts(tool_results, [_LIST_ISSUES_SPEC])
    assert exc_info.value.code == FACT_EXTRACTION_FAILED
    assert "returned an error (not JSON)" in exc_info.value.message
    assert exc_info.value.tool_result_preview is not None
    assert "Could not resolve" in exc_info.value.tool_result_preview


def test_extract_step_facts_large_json_payload() -> None:
    large_issues = [{"body": "x" * 8500}]
    payload = json.dumps({"issues": large_issues, "totalCount": 1})
    assert len(payload) > DEFAULT_TOOL_RESULT_RECORD_LIMIT
    tool_results = [{"tool": "list_issues", "result": payload}]
    facts = extract_step_facts(tool_results, [_LIST_ISSUES_SPEC])
    assert facts["list_issues"]["total_count"] == 1


def test_extract_step_facts_detects_truncated_json_at_record_limit() -> None:
    truncated = ('{"totalCount": 1, "issues": [{"body": "' + ("y" * 9000))[
        :DEFAULT_TOOL_RESULT_RECORD_LIMIT
    ]
    assert len(truncated) == DEFAULT_TOOL_RESULT_RECORD_LIMIT
    tool_results = [{"tool": "list_issues", "result": truncated}]
    with pytest.raises(StepFactsExtractionError) as exc_info:
        extract_step_facts(tool_results, [_LIST_ISSUES_SPEC])
    assert exc_info.value.code == TOOL_RESULT_TRUNCATED
    assert exc_info.value.truncation_limit == DEFAULT_TOOL_RESULT_RECORD_LIMIT
    assert "truncated at the worker record limit" in exc_info.value.message


def test_validate_facts_extractors_rejects_duplicate_into() -> None:
    specs = [
        {"tool": "a", "into": "x", "fields": {"f": "$.f"}},
        {"tool": "b", "into": "x", "fields": {"g": "$.g"}},
    ]
    with pytest.raises(ValueError, match="duplicate facts.into"):
        validate_facts_extractors(specs)


def test_validate_facts_extractors_rejects_bad_jsonpath() -> None:
    specs = [{"tool": "t", "into": "bucket", "fields": {"f": "$.[not valid"}}]
    with pytest.raises(ValueError, match="invalid JSONPath"):
        validate_facts_extractors(specs)
