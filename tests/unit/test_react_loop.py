"""Unit tests for the native ReAct loop (workers.adapters.react_loop)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from common.agent_adapter import ExecutionStepError
from common.llm import ChatMessage, ChatResponse, ToolCall
from common.utils import tool_call_args_to_dict
from mcp.types import Tool as McpTool
from workers.adapters.react_loop import (
    ReactLoopResult,
    _collect_last_tool_errors,
    parse_compensation_output,
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
        completion_mode="submit",
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
        completion_mode="submit",
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
        completion_mode="submit",
        max_turns=5,
    )
    assert result.submit_payload == {"summary": "Done.", "count": 2}
    assert len(result.transcript) == 2


@pytest.mark.asyncio
async def test_submit_mode_raises_when_no_submit():
    llm = _ScriptedLLM([ChatResponse(content='{"summary": "nope"}')])
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[],
            allowed_tool_names=[],
            completion_mode="submit",
            max_turns=5,
        )
    assert "no_submit_call" in str(exc_info.value.error_details or {})
    details = exc_info.value.error_details or {}
    assert details.get("code") == "no_submit_call"
    assert details.get("reason") == "model_text_exit"
    assert details.get("message")
    assert '{"summary": "nope"}' in str(details.get("last_assistant_content") or "")


@pytest.mark.asyncio
async def test_submit_mode_model_text_exit_ignores_plain_text_tool_success():
    mock_tool = MagicMock()
    mock_tool.name = "sandbox_write"
    mock_tool.ainvoke = AsyncMock(return_value="Successfully wrote file to /tmp/foo")

    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="sandbox_write", args={}, id="1")]),
            ChatResponse(content="I completed the sandbox work but forgot _submit."),
        ]
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=[ChatMessage(role="human", content="go")],
            mcp_tools=[mock_tool],
            allowed_tool_names=["sandbox_write"],
            completion_mode="submit",
            max_turns=5,
        )
    details = exc_info.value.error_details or {}
    assert details.get("reason") == "model_text_exit"
    assert not details.get("last_tool_errors")
    assert "forgot _submit" in str(details.get("last_assistant_content") or "")


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
            completion_mode="submit",
            max_turns=5,
        )
    details = exc_info.value.error_details or {}
    assert details.get("code") == "TOOL_OUTPUT_ERROR"


@pytest.mark.asyncio
async def test_submit_mode_empty_submit_payload():
    llm = _ScriptedLLM([ChatResponse(tool_calls=[ToolCall(name="_submit", args={}, id="1")])])
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        completion_mode="submit",
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
            completion_mode="submit",
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
            completion_mode="submit",
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
        completion_mode="submit",
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
        completion_mode="submit",
        max_turns=10,
    )
    assert result.submit_payload == {"summary": "done"}
    mock_session.call_tool.assert_called_once_with(
        "sandbox_exec",
        arguments={"commands": ["ok"]},
    )


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
        completion_mode="submit",
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
        completion_mode="submit",
        max_turns=10,
    )
    assert result.tool_results is not None
    assert result.tool_results[0]["result"] == large_payload
    tool_messages = [m for m in result.transcript if m.role == "tool"]
    assert len(tool_messages) == 1
    assert len(tool_messages[0].content or "") <= 8000
    assert "_warden_clipped" in (tool_messages[0].content or "")


@pytest.mark.asyncio
async def test_assistant_json_mode_parses_final_content():
    llm = _ScriptedLLM([ChatResponse(content='{"rollback": "done"}')])
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        completion_mode="assistant_json",
        max_turns=5,
    )
    assert result.final_content == '{"rollback": "done"}'


@pytest.mark.asyncio
async def test_assistant_json_synthetic_when_tools_but_no_final_json():
    mock_tool = MagicMock()
    mock_tool.name = "undo"
    mock_tool.ainvoke = AsyncMock(return_value="ok")

    llm = _ScriptedLLM(
        [
            ChatResponse(tool_calls=[ToolCall(name="undo", args={}, id="1")]),
            ChatResponse(content=""),
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["undo"],
        completion_mode="assistant_json",
        max_turns=2,
    )
    assert result.tool_results is not None
    assert len(result.tool_results) == 1


@pytest.mark.asyncio
async def test_assistant_json_max_turns_synthetic_with_tool_results():
    mock_tool = MagicMock()
    mock_tool.name = "undo"
    mock_tool.ainvoke = AsyncMock(return_value="ok")
    llm = _ScriptedLLM(
        [ChatResponse(tool_calls=[ToolCall(name="undo", args={}, id="1")])],
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[mock_tool],
        allowed_tool_names=["undo"],
        completion_mode="assistant_json",
        max_turns=1,
    )
    assert result.tool_results is not None


def test_tool_call_args_to_dict_nested_and_scalar():
    """Shared normalizer preserves nested dicts; wraps non-dict primitives."""
    nested = {"payment_id": "pay-1", "meta": {"amount": 100}}
    assert tool_call_args_to_dict(nested) == nested
    assert tool_call_args_to_dict(42) == {"value": 42}
    assert tool_call_args_to_dict(None) == {}


@pytest.mark.asyncio
async def test_assistant_json_prose_without_tool_calls_returns_immediately():
    """assistant_json exits on first non-tool response (legacy compensation behavior)."""
    llm = _ScriptedLLM([ChatResponse(content='{"rollback": "done"}')])
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="go")],
        mcp_tools=[],
        allowed_tool_names=[],
        completion_mode="assistant_json",
        max_turns=10,
    )
    assert result.final_content == '{"rollback": "done"}'


@pytest.mark.asyncio
async def test_assistant_json_zero_max_turns_raises_execution_step_error():
    """Exhausting the turn budget with no tool rounds uses ExecutionStepError, not ValueError."""
    initial_messages = [ChatMessage(role="human", content="go")]
    llm = _ScriptedLLM([])
    with pytest.raises(ExecutionStepError) as exc_info:
        await run_react_loop(
            llm=llm,
            initial_messages=initial_messages,
            mcp_tools=[],
            allowed_tool_names=[],
            completion_mode="assistant_json",
            max_turns=0,
        )
    details = exc_info.value.error_details or {}
    assert details.get("code") == "compensation_max_turns"
    assert details.get("had_tool_results") is False
    assert details.get("transcript_message_count") == len(initial_messages)


def test_parse_compensation_output_synthetic():
    result = ReactLoopResult(
        transcript=[],
        tool_results=[{"tool": "a", "result": "ok"}],
        final_content=None,
    )
    out = parse_compensation_output(result)
    assert out["rollback_status"] == "completed"


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
            completion_mode="submit",
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
async def test_compensation_mode_ignores_token_budget_when_none():
    """Compensation passes max_step_tokens=None; loop must not abort on tokens."""
    from common.execution_usage import WorkerUsageAccumulator
    from common.llm import TokenUsage

    usage_acc = WorkerUsageAccumulator()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content='{"rollback_status": "completed"}',
                usage=TokenUsage(prompt_tokens=500, completion_tokens=50, total_tokens=550),
            )
        ]
    )
    result = await run_react_loop(
        llm=llm,
        initial_messages=[ChatMessage(role="human", content="undo")],
        mcp_tools=[],
        allowed_tool_names=[],
        completion_mode="assistant_json",
        max_turns=5,
        usage_acc=usage_acc,
        max_step_tokens=None,
    )
    assert result.final_content is not None
    assert usage_acc.total_tokens == 550
