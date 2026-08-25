"""Unit tests for the native ReAct loop (workers.adapters.react_loop)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from common.agent_adapter import ExecutionStepError
from common.llm import ChatMessage, ChatResponse, ToolCall
from common.tool_arg_bind import overlay_bound_tool_arguments
from common.utils import tool_call_args_to_dict
from mcp.types import Tool as McpTool
from workers.adapters.react_loop import (
    _collect_last_tool_errors,
    run_react_loop,
)
from workers.tools import _convert_mcp_to_langchain


class _ScriptedLLM:
    """Minimal ChatModelPort test double."""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)

    def bind_tools(self, tools: object) -> _ScriptedLLM:
        return self

    async def ainvoke(self, messages: list[ChatMessage]) -> ChatResponse:
        if not self._responses:
            raise RuntimeError("no more scripted responses")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_memory_compression_toggle_skips_compress(monkeypatch):
    """WARDEN_REACT_MEMORY_COMPRESSION=0 must not call compress_if_needed."""
    calls: list[int] = []

    def _track(messages, **kwargs):
        from workers.adapters.react_memory import CompressionStats

        calls.append(len(messages))
        return list(messages), CompressionStats()

    monkeypatch.setattr("workers.adapters.react_loop.compress_if_needed", _track)
    monkeypatch.setenv("WARDEN_REACT_MEMORY_COMPRESSION", "0")

    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"ok": True}},
                        id="1",
                    )
                ]
            )
        ]
    )
    await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=5,
    )
    assert calls == []

    monkeypatch.setenv("WARDEN_REACT_MEMORY_COMPRESSION", "1")
    llm2 = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"ok": True}},
                        id="2",
                    )
                ]
            )
        ]
    )
    await run_react_loop(
        llm=llm2,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=5,
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_submit_mode_returns_payload_on_submit_call():
    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "Done.", "count": 2}},
                        id="1",
                    )
                ]
            )
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=5,
    )
    assert result.submit_payload == {"summary": "Done.", "count": 2}
    assert len(result.transcript) == 2


@pytest.mark.asyncio
async def test_submit_mode_raises_when_no_submit(monkeypatch):
    """After one soft retry, a second text-only exit still fails closed."""
    monkeypatch.setenv("WARDEN_REACT_SUBMIT_TEXT_RETRIES", "1")
    llm = _ScriptedLLM(
        [
            ChatResponse(content='{"summary": "nope"}'),
            ChatResponse(content="still no tool call"),
        ]
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[],
            allowed_tool_names=[],
            max_turns=5,
        )
    assert "no_submit_call" in str(exc_info.value.error_details or {})
    details = exc_info.value.error_details or {}
    assert details.get("code") == "no_submit_call"
    assert details.get("reason") == "model_text_exit"
    assert details.get("message")
    assert "still no tool call" in str(details.get("last_assistant_content") or "")


@pytest.mark.asyncio
async def test_submit_mode_text_exit_soft_retry_then_submit(monkeypatch):
    monkeypatch.setenv("WARDEN_REACT_SUBMIT_TEXT_RETRIES", "1")
    llm = _ScriptedLLM(
        [
            ChatResponse(content="**Bug**: I forgot to call the tool."),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "recovered", "feasible": True}},
                        id="1",
                    )
                ]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=5,
    )
    assert result.submit_payload == {"summary": "recovered", "feasible": True}
    # human nudge injected between prose assistant turn and successful _submit
    assert any(
        m.role == "human" and "_submit" in (m.content or "") and "plain text" in (m.content or "")
        for m in result.transcript
    )


@pytest.mark.asyncio
async def test_submit_mode_text_exit_soft_retry_disabled(monkeypatch):
    monkeypatch.setenv("WARDEN_REACT_SUBMIT_TEXT_RETRIES", "0")
    llm = _ScriptedLLM([ChatResponse(content='{"summary": "nope"}')])
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[],
            allowed_tool_names=[],
            max_turns=5,
        )
    details = exc_info.value.error_details or {}
    assert details.get("reason") == "model_text_exit"
    assert '{"summary": "nope"}' in str(details.get("last_assistant_content") or "")


@pytest.mark.asyncio
async def test_submit_mode_text_exit_soft_retry_on_final_turn(monkeypatch):
    """Prose exit on the last scheduled turn still gets one bonus recovery call."""
    monkeypatch.setenv("WARDEN_REACT_SUBMIT_TEXT_RETRIES", "1")
    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="_submit", args={"result": {"n": 1}}, id="1")]),
        ]
    )
    # Warm path sanity: max_turns=1 with immediate submit still works
    ok = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=1,
    )
    assert ok.submit_payload == {"n": 1}

    llm2 = _ScriptedLLM(
        [
            ChatResponse(content="narrating instead of submitting"),
            ChatResponse(
                tool_calls=[
                    ToolCall(name="_submit", args={"result": {"n": 2}}, id="2"),
                ]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm2,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=1,
    )
    assert result.submit_payload == {"n": 2}


@pytest.mark.asyncio
async def test_submit_mode_mixed_batch_runs_tools_and_defers_submit():
    """`_submit` with other tools must not exit; non-submit tools still run."""
    mock_tool = MagicMock()
    mock_tool.name = "sandbox_write"
    mock_tool.ainvoke = AsyncMock(return_value="wrote /testbed/foo")

    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(name="sandbox_write", args={"path": "/testbed/foo"}, id="1"),
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "premature"}},
                        id="2",
                    ),
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "after review"}},
                        id="3",
                    )
                ]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["sandbox_write"],
        max_turns=5,
    )
    assert result.submit_payload == {"summary": "after review"}
    mock_tool.ainvoke.assert_called_once()
    submit_rejects = [
        m
        for m in result.transcript
        if m.role == "tool"
        and m.name == "_submit"
        and "must be the only tool call" in (m.content or "")
    ]
    assert len(submit_rejects) == 1


@pytest.mark.asyncio
async def test_submit_mode_mixed_batch_submit_first_still_runs_other_tools():
    mock_tool = MagicMock()
    mock_tool.name = "sandbox_write"
    mock_tool.ainvoke = AsyncMock(return_value="wrote /testbed/bar")

    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "too early"}},
                        id="1",
                    ),
                    ToolCall(name="sandbox_write", args={}, id="2"),
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "ok"}},
                        id="3",
                    )
                ]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["sandbox_write"],
        max_turns=5,
    )
    assert result.submit_payload == {"summary": "ok"}
    mock_tool.ainvoke.assert_called_once()


@pytest.mark.asyncio
async def test_submit_mode_model_text_exit_ignores_plain_text_tool_success(monkeypatch):
    monkeypatch.setenv("WARDEN_REACT_SUBMIT_TEXT_RETRIES", "1")
    mock_tool = MagicMock()
    mock_tool.name = "sandbox_write"
    mock_tool.ainvoke = AsyncMock(return_value="Successfully wrote file to /tmp/foo")

    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="sandbox_write", args={}, id="1")]),
            ChatResponse(content="I completed the sandbox work but forgot _submit."),
            ChatResponse(content="still forgot _submit after nudge"),
        ]
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[mock_tool],
            allowed_tool_names=["sandbox_write"],
            max_turns=5,
        )
    details = exc_info.value.error_details or {}
    assert details.get("reason") == "model_text_exit"
    assert not details.get("last_tool_errors")
    assert "still forgot _submit" in str(details.get("last_assistant_content") or "")


def test_collect_last_tool_errors_skips_plain_text_success():
    tool_results = [
        {"tool": "sandbox_write", "result": "Successfully wrote file to /tmp/foo"},
        {"tool": "lookup", "result": "MCP error: connection refused"},
    ]
    errors = _collect_last_tool_errors(tool_results)
    assert len(errors) == 1
    assert errors[0]["tool"] == "lookup"
    assert "MCP error: connection refused" in errors[0]["preview"]


@pytest.mark.asyncio
async def test_submit_mode_tool_failure_raises_before_no_submit():
    mock_tool = MagicMock()
    mock_tool.name = "sandbox_write"
    mock_tool.ainvoke = AsyncMock(return_value="MCP error: connection refused")

    llm = _ScriptedLLM([ChatResponse(tool_calls=[ToolCall(name="sandbox_write", args={}, id="1")])])
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[mock_tool],
            allowed_tool_names=["sandbox_write"],
            max_turns=5,
        )
    details = exc_info.value.error_details or {}
    assert details.get("code") == "TOOL_OUTPUT_ERROR"


@pytest.mark.asyncio
async def test_submit_mode_recoverable_edit_mismatch_feeds_back_then_submit():
    """search_replace-style mismatches must not abort the step; model gets another turn."""
    mock_tool = MagicMock()
    mock_tool.name = "search_replace_sandbox"
    mock_tool.ainvoke = AsyncMock(
        side_effect=[
            "Error: old_text not found in /testbed/lib/matplotlib/__init__.py",
            "ok",
        ]
    )

    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="search_replace_sandbox",
                        args={"path": "/x", "old_text": "a", "new_text": "b"},
                        id="1",
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="search_replace_sandbox",
                        args={"path": "/x", "old_text": "aa", "new_text": "b"},
                        id="2",
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"ok": True}}, id="3")]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["search_replace_sandbox"],
        max_turns=5,
    )
    assert result.submit_payload == {"ok": True}
    assert mock_tool.ainvoke.await_count == 2
    tool_msgs = [m for m in result.transcript if m.role == "tool"]
    assert any("old_text not found" in (m.content or "") for m in tool_msgs)
    assert any("read_file_sandbox" in (m.content or "") for m in tool_msgs)


@pytest.mark.asyncio
async def test_submit_mode_invoke_exception_directory_softens_then_submit():
    mock_tool = MagicMock()
    mock_tool.name = "read_file_sandbox"
    mock_tool.ainvoke = AsyncMock(
        side_effect=[
            IsADirectoryError("/testbed/docs"),
            "file contents",
        ]
    )

    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(name="read_file_sandbox", args={"path": "/testbed/docs"}, id="1")
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="read_file_sandbox", args={"path": "/testbed/docs/conf.py"}, id="2"
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"ok": True}}, id="3")]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["read_file_sandbox"],
        max_turns=5,
    )
    assert result.submit_payload == {"ok": True}
    assert mock_tool.ainvoke.await_count == 2
    tool_msgs = [m for m in result.transcript if m.role == "tool"]
    assert any("IsADirectoryError" in (m.content or "") for m in tool_msgs)
    assert any("list_dir_sandbox" in (m.content or "") for m in tool_msgs)


@pytest.mark.asyncio
async def test_submit_mode_invoke_infrastructure_exception_raises():
    mock_tool = MagicMock()
    mock_tool.name = "read_file_sandbox"
    mock_tool.ainvoke = AsyncMock(side_effect=ConnectionError("Connection refused"))

    llm = _ScriptedLLM(
        [ChatResponse(tool_calls=[ToolCall(name="read_file_sandbox", args={}, id="1")])]
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[mock_tool],
            allowed_tool_names=["read_file_sandbox"],
            max_turns=5,
        )
    assert (exc_info.value.error_details or {}).get("code") == "TOOL_INVOKE_FAILED"


@pytest.mark.asyncio
async def test_submit_mode_cancelled_error_propagates():
    import asyncio

    mock_tool = MagicMock()
    mock_tool.name = "echo"
    mock_tool.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())

    llm = _ScriptedLLM([ChatResponse(tool_calls=[ToolCall(name="echo", args={}, id="1")])])
    with pytest.raises(asyncio.CancelledError):
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[mock_tool],
            allowed_tool_names=["echo"],
            max_turns=5,
        )


@pytest.mark.asyncio
async def test_submit_mode_empty_submit_payload():
    llm = _ScriptedLLM([ChatResponse(tool_calls=[ToolCall(name="_submit", args={}, id="1")])])
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=5,
    )
    assert result.submit_payload == {}


@pytest.mark.asyncio
async def test_submit_mode_disallowed_tool_raises():
    llm = _ScriptedLLM([ChatResponse(tool_calls=[ToolCall(name="bad_tool", args={}, id="1")])])
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[],
            allowed_tool_names=["allowed_only"],
            max_turns=5,
        )
    assert exc_info.value.tool == "bad_tool"


@pytest.mark.asyncio
async def test_submit_mode_tool_failure_raises():
    mock_tool = MagicMock()
    mock_tool.name = "some_tool"
    mock_tool.ainvoke = AsyncMock(return_value="MCP error: connection refused")

    llm = _ScriptedLLM([ChatResponse(tool_calls=[ToolCall(name="some_tool", args={}, id="1")])])
    with pytest.raises(ExecutionStepError):
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[mock_tool],
            allowed_tool_names=["some_tool"],
            max_turns=5,
        )


@pytest.mark.asyncio
async def test_mcp_tool_round_trip_then_submit():
    mock_tool = MagicMock()
    mock_tool.name = "lookup"
    mock_tool.ainvoke = AsyncMock(return_value='{"ok": true}')

    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="lookup", args={"id": "1"}, id="1")]),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"summary": "done"}}, id="2")]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["lookup"],
        max_turns=10,
    )
    assert result.submit_payload == {"summary": "done"}
    assert result.tool_results == [{"tool": "lookup", "result": '{"ok": true}'}]
    mock_tool.ainvoke.assert_called_once()


@pytest.mark.asyncio
async def test_react_loop_coerces_stringified_array_args_before_ainvoke():
    """ReAct loop normalizes sloppy LLM tool args against MCP inputSchema before invoke."""
    mcp_tool = McpTool(
        name="sandbox_exec",
        description="Run commands",
        inputSchema={
            "type": "object",
            "properties": {
                "commands": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["commands"],
        },
    )
    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(
        return_value=MagicMock(content=[MagicMock(type="text", text="ok")]),
    )
    tool = _convert_mcp_to_langchain(mcp_tool, mock_session, step_spec=None)

    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="sandbox_exec",
                        args={"commands": '["ok"]'},
                        id="1",
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"summary": "done"}}, id="2")]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[tool],
        allowed_tool_names=["sandbox_exec"],
        max_turns=10,
    )
    assert result.submit_payload == {"summary": "done"}
    mock_session.call_tool.assert_called_once_with(
        "sandbox_exec",
        arguments={"commands": ["ok"]},
    )


@pytest.mark.asyncio
async def test_react_loop_merge_tool_args_overlays_bound_container_id():
    """Saga-wins merge_tool_args pins container_id before MCP invoke."""

    mcp_tool = McpTool(
        name="sandbox_exec",
        description="Run",
        inputSchema={
            "type": "object",
            "properties": {
                "container_id": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": ["container_id", "command"],
        },
    )
    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(
        return_value=MagicMock(content=[MagicMock(type="text", text="ok")]),
    )
    tool = _convert_mcp_to_langchain(
        mcp_tool,
        mock_session,
        step_spec=None,
        omit_arg_keys=["container_id"],
    )

    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="sandbox_exec",
                        args={"container_id": "llm-junk", "command": "ls"},
                        id="1",
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"summary": "done"}}, id="2")]
            ),
        ]
    )
    await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[tool],
        allowed_tool_names=["sandbox_exec"],
        max_turns=10,
        merge_context={"container_id": "saga-real-id"},
        merge_tool_args=overlay_bound_tool_arguments,
    )
    mock_session.call_tool.assert_called_once_with(
        "sandbox_exec",
        arguments={"container_id": "saga-real-id", "command": "ls"},
    )


@pytest.mark.asyncio
async def test_react_loop_sanitized_llm_name_records_original_mcp_in_tool_results():
    """LLM calls sanitized name; call_tool and facts tool_results use original MCP id."""
    mcp_tool = McpTool(
        name="calendar.list_events",
        description="List",
        inputSchema={"type": "object", "properties": {}, "required": []},
    )
    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(
        return_value=MagicMock(content=[MagicMock(type="text", text='{"ok": true}')]),
    )
    tool = _convert_mcp_to_langchain(mcp_tool, mock_session, step_spec=None)
    assert tool.name == "calendar_list_events"

    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="calendar_list_events", args={}, id="1")]),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"summary": "done"}}, id="2")]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[tool],
        allowed_tool_names=["calendar_list_events"],
        max_turns=10,
    )
    assert result.tool_results == [
        {"tool": "calendar.list_events", "result": '{"ok": true}'},
    ]
    mock_session.call_tool.assert_called_once_with("calendar.list_events", arguments={})


@pytest.mark.asyncio
async def test_tool_results_store_full_payload_without_truncation():
    large_payload = '{"totalCount": 1, "issues": [{"body": "' + ("z" * 8500) + '"}]}'
    mock_tool = MagicMock()
    mock_tool.name = "lookup"
    mock_tool.ainvoke = AsyncMock(return_value=large_payload)

    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="lookup", args={"id": "1"}, id="1")]),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"summary": "done"}}, id="2")]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["lookup"],
        max_turns=10,
    )
    assert result.tool_results is not None
    assert result.tool_results[0]["result"] == large_payload
    assert len(result.tool_results[0]["result"]) > 8000


@pytest.mark.asyncio
async def test_llm_tool_message_clipped_while_tool_results_stay_full(monkeypatch):
    monkeypatch.delenv("WARDEN_REACT_TOOL_MESSAGE_LIMIT", raising=False)
    large_payload = (
        '{"totalCount": 2, "issues": ['
        + ",".join([json.dumps({"body": "b" * 3000}) for _ in range(6)])
        + "]}"
    )
    assert len(large_payload) > 8000
    mock_tool = MagicMock()
    mock_tool.name = "lookup"
    mock_tool.ainvoke = AsyncMock(return_value=large_payload)

    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="lookup", args={"id": "1"}, id="1")]),
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"summary": "done"}}, id="2")]
            ),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["lookup"],
        max_turns=10,
    )
    assert result.tool_results is not None
    assert result.tool_results[0]["result"] == large_payload
    tool_messages = [m for m in result.transcript if m.role == "tool"]
    assert len(tool_messages) == 1
    assert len(tool_messages[0].content or "") <= 8000
    assert "_warden_clipped" in (tool_messages[0].content or "")


def test_tool_call_args_to_dict_nested_and_scalar():
    """Shared normalizer preserves nested dicts; wraps non-dict primitives."""
    nested = {"payment_id": "pay-1", "meta": {"amount": 100}}
    assert tool_call_args_to_dict(nested) == nested
    assert tool_call_args_to_dict(42) == {"value": 42}
    assert tool_call_args_to_dict(None) == {}


@pytest.mark.asyncio
async def test_zero_max_turns_raises_no_submit():
    """Exhausting the turn budget with no tool rounds uses no_submit_call."""
    initial_messages = [ChatMessage(role="human", content="go")]
    llm = _ScriptedLLM([])
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=initial_messages,
            mcp_tools=[],
            allowed_tool_names=[],
            max_turns=0,
        )
    details = exc_info.value.error_details or {}
    assert details.get("code") == "no_submit_call"
    assert details.get("reason") == "max_turns_exceeded"


@pytest.mark.asyncio
async def test_submit_mode_raises_when_step_token_budget_exceeded():
    from common.execution_usage import WorkerUsageAccumulator
    from common.llm import TokenUsage

    usage_acc = WorkerUsageAccumulator()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[ToolCall(name="sandbox_write", args={}, id="1")],
                usage=TokenUsage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "done"}},
                        id="2",
                    )
                ],
                usage=TokenUsage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
            ),
        ]
    )
    mock_tool = MagicMock()
    mock_tool.name = "sandbox_write"
    mock_tool.ainvoke = AsyncMock(return_value="ok")

    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[mock_tool],
            allowed_tool_names=["sandbox_write"],
            max_turns=5,
            usage_acc=usage_acc,
            max_step_tokens=60,
        )
    details = exc_info.value.error_details or {}
    assert details.get("code") == "STEP_TOKEN_LIMIT_EXCEEDED"
    assert details.get("tokens_used") == 100
    assert details.get("max_step_tokens") == 60
    assert details.get("prompt_tokens") == 80
    assert details.get("completion_tokens") == 20
    assert usage_acc.total_tokens == 100


@pytest.mark.asyncio
async def test_unlimited_token_budget_when_max_step_tokens_none():
    """max_step_tokens=None must not abort on large provider totals."""
    from common.execution_usage import WorkerUsageAccumulator
    from common.llm import TokenUsage

    usage_acc = WorkerUsageAccumulator()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "done"}},
                        id="1",
                    )
                ],
                usage=TokenUsage(prompt_tokens=500, completion_tokens=50, total_tokens=550),
            )
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        max_turns=5,
        usage_acc=usage_acc,
        max_step_tokens=None,
    )
    assert result.submit_payload == {"summary": "done"}
    assert usage_acc.total_tokens == 550
