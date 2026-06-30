"""Unit tests for workers.adapters.state_utils (kernel transcript helpers)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from common.agent_adapter import ExecutionStepError
from common.llm import ChatMessage
from workers.adapters.state_utils import (
    check_allowlist_in_state,
    check_tool_failures_in_state,
    extract_submit_payload,
    fallback_output_from_last_assistant_content,
    parse_json_object_from_assistant_text,
)


def test_fallback_output_from_last_assistant_content_wraps_non_json_prose():
    msg = MagicMock(type="ai", content="not-json")
    result = fallback_output_from_last_assistant_content([msg])
    assert result == {"summary": "not-json"}


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([ChatMessage(role="assistant", content='{"ok": true}')], {"ok": True}),
        ([ChatMessage(role="assistant", content="plain prose")], {"summary": "plain prose"}),
        ([ChatMessage(role="assistant", content="")], ExecutionStepError),
        ([], ExecutionStepError),
    ],
    ids=["json-object", "prose-wrap", "empty-content", "no-messages"],
)
def test_fallback_output_from_last_assistant_content(messages, expected):
    if expected is ExecutionStepError:
        with pytest.raises(ExecutionStepError, match="no assistant content"):
            fallback_output_from_last_assistant_content(messages)
        return
    assert fallback_output_from_last_assistant_content(messages) == expected


def test_fallback_output_uses_last_assistant_message():
    messages = [
        ChatMessage(role="assistant", content='{"first": 1}'),
        ChatMessage(role="assistant", content='{"last": 2}'),
    ]
    assert fallback_output_from_last_assistant_content(messages) == {"last": 2}


def test_fallback_output_truncates_massive_prose():
    huge = "x" * 20000
    result = fallback_output_from_last_assistant_content(
        [ChatMessage(role="assistant", content=huge)]
    )
    assert result == {"summary": "x" * 10000}


def test_fallback_output_non_dict_json_wraps_summary():
    result = fallback_output_from_last_assistant_content(
        [ChatMessage(role="assistant", content='["not", "a", "dict"]')]
    )
    assert result == {"summary": '["not", "a", "dict"]'}


def test_fallback_output_deep_nested_json():
    nested = {"a": {"b": {"c": {"d": list(range(50))}}}}
    msg = ChatMessage(role="assistant", content=__import__("json").dumps(nested))
    assert fallback_output_from_last_assistant_content([msg]) == nested


def test_parse_json_object_from_assistant_text_strips_fence():
    assert parse_json_object_from_assistant_text('```json\n{"summary": "x"}\n```') == {
        "summary": "x"
    }


def test_extract_submit_payload_from_dict_tool_calls():
    msg = SimpleNamespace(
        tool_calls=[{"name": "_submit", "args": {"result": {"done": True}}}],
    )
    assert extract_submit_payload([msg]) == {"done": True}


def test_extract_submit_payload_prefers_latest_submit():
    older = SimpleNamespace(tool_calls=[{"name": "_submit", "args": {"result": {"n": 1}}}])
    newer = SimpleNamespace(tool_calls=[{"name": "_submit", "args": {"result": {"n": 2}}}])
    assert extract_submit_payload([older, newer]) == {"n": 2}


def test_extract_submit_payload_returns_empty_dict_for_non_dict_args():
    msg = SimpleNamespace(tool_calls=[{"name": "_submit", "args": "bad"}])
    assert extract_submit_payload([msg]) == {}


def test_extract_submit_payload_none_when_missing():
    assert extract_submit_payload([]) is None
    assert extract_submit_payload([ChatMessage(role="assistant", content="hi")]) is None


def test_check_allowlist_in_state_allows_submit_and_listed_tools():
    msg = SimpleNamespace(tool_calls=[{"name": "lookup", "args": {}}])
    check_allowlist_in_state([msg], allowed_tool_names=["lookup"])


def test_check_allowlist_in_state_rejects_disallowed_tool():
    msg = SimpleNamespace(tool_calls=[{"name": "rm_rf", "args": {}}])
    with pytest.raises(ExecutionStepError, match="not in allowlist"):
        check_allowlist_in_state([msg], allowed_tool_names=["lookup"])


def test_check_allowlist_in_state_ignores_empty_tool_name():
    msg = SimpleNamespace(tool_calls=[{"name": "", "args": {}}])
    check_allowlist_in_state([msg], allowed_tool_names=[])


def test_check_tool_failures_in_state_raises_on_failure_output(mocker):
    mocker.patch(
        "workers.adapters.state_utils.tool_output_indicates_failure",
        return_value=True,
    )
    msg = ChatMessage(role="tool", content='{"error": "boom"}')
    with pytest.raises(ExecutionStepError, match="boom"):
        check_tool_failures_in_state([msg])


def test_check_tool_failures_in_state_ignores_success_output(mocker):
    mocker.patch(
        "workers.adapters.state_utils.tool_output_indicates_failure",
        return_value=False,
    )
    msg = ChatMessage(role="tool", content='{"ok": true}')
    check_tool_failures_in_state([msg])


def test_check_tool_failures_in_state_truncates_long_error(mocker):
    mocker.patch(
        "workers.adapters.state_utils.tool_output_indicates_failure",
        return_value=True,
    )
    long_err = "e" * 5000
    msg = ChatMessage(role="tool", content=long_err)
    with pytest.raises(ExecutionStepError) as exc:
        check_tool_failures_in_state([msg])
    assert len(str(exc.value)) <= 1000
    assert exc.value.error_details is not None
    assert len(exc.value.error_details["error"]) <= 2000
