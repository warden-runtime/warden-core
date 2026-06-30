"""Unit tests for the agent adapter (native ReAct loop + _submit)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from common.agent_adapter import ExecutionStepError
from common.compensation_context import (
    WARDEN_TOOL_IDEMPOTENCY_KEY,
    merge_compensation_tool_arguments,
)
from common.llm import ChatResponse, ToolCall
from workers.adapters.langchain import LangChainAdapter


class _ScriptedLLM:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)

    def bind_tools(self, tools: object) -> _ScriptedLLM:
        return self

    async def ainvoke(self, messages: object) -> ChatResponse:
        return self._responses.pop(0)


def _make_worker_def(
    model_provider: str = "openai",
    model_name: str = "gpt-4o",
    system_prompt: str = "You are helpful.",
    tool_sources: list | None = None,
    compensation_prompt: str | None = None,
):
    w = MagicMock()
    w.name = "test-worker"
    w.version = "1.0.0"
    w.adapter = "langchain"
    w.model_provider = model_provider
    w.model_name = model_name
    w.system_prompt = system_prompt
    w.tool_sources = tool_sources or []
    w.compensation_prompt = compensation_prompt
    return w


def test_merge_compensation_tool_args_keeps_resolved_ids_when_llm_sends_null():
    """Engine-resolved original_input must win over null/empty LLM tool args."""
    merged = merge_compensation_tool_arguments(
        {"payment_id": None, "reservation_id": "res-000001"},
        {
            "payment_id": "pay-000001",
            "reservation_id": "res-000001",
            "amount_cents": 100,
        },
    )
    assert merged["payment_id"] == "pay-000001"
    assert merged["reservation_id"] == "res-000001"
    assert merged["amount_cents"] == 100


def test_merge_compensation_tool_args_injects_idempotency_key():
    merged = merge_compensation_tool_arguments(
        {},
        {"payment_id": "pay-1"},
        idempotency_key="comp-trace-span",
    )
    assert merged[WARDEN_TOOL_IDEMPOTENCY_KEY] == "comp-trace-span"
    assert merged["payment_id"] == "pay-1"


def _make_secret(api_key: str = "sk-fake"):
    s = MagicMock()
    s.api_key = api_key
    return s


def _patch_build_llm(mocker, responses: list[ChatResponse]):
    return mocker.patch(
        "workers.adapters.langchain.build_llm",
        return_value=_ScriptedLLM(responses),
    )


@pytest.mark.asyncio
async def test_run_step_returns_submit_payload_when_agent_calls_submit(mocker):
    _patch_build_llm(
        mocker,
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
        ],
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    result = await adapter.run_step(
        system_prompt="You are helpful.",
        prompt_template="Do the task.",
        arguments={},
        tool_specs=[],
        context={},
    )
    assert result.output == {"data": {"summary": "Done.", "count": 2}}


@pytest.mark.asyncio
async def test_run_step_simple_adapter_returns_structured_output(mocker):
    mocker.patch(
        "workers.adapters.langchain.invoke_structured_output",
        new_callable=AsyncMock,
        return_value={"summary": "ok"},
    )
    mocker.patch("workers.adapters.langchain.build_llm")

    mock_registry = MagicMock()
    mock_registry.tools.on_allowlist_passed = AsyncMock()
    mock_registry.adapter.after_reason_step = AsyncMock()
    mocker.patch("workers.adapters.langchain.get_registry", return_value=mock_registry)

    scope = MagicMock()
    scope.namespace = "default"
    scope.trace_id = "trace"
    scope.step_span_id = "step"
    scope.idempotency_key = "idem"
    mocker.patch(
        "workers.adapters.langchain.execution_scope_from_injection",
        return_value=scope,
    )
    mocker.patch("workers.adapters.langchain.db_conn_from_injection", return_value=None)

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    result = await adapter.run_step(
        system_prompt="You are helpful.",
        prompt_template="Connectivity check.",
        arguments={},
        tool_specs=[],
        context={"execution_scope": scope},
        agent_adapter="simple",
    )
    assert result.output == {"data": {"summary": "ok"}}
    mock_registry.adapter.after_reason_step.assert_called_once()
    hook_kwargs = mock_registry.adapter.after_reason_step.call_args.kwargs
    assert hook_kwargs["submit_payload"] == {"summary": "ok"}
    assert any(m.role == "assistant" for m in hook_kwargs["messages"])


@pytest.mark.asyncio
async def test_run_step_raises_when_no_submit(mocker):
    _patch_build_llm(mocker, [ChatResponse(content='{"summary": "Fallback result."}')])
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Do the task.",
            arguments={},
            tool_specs=[],
            context={},
        )
    assert "no_submit_call" in str(exc_info.value.error_details or {})


@pytest.mark.asyncio
async def test_run_step_raises_when_no_submit_and_prose_content(mocker):
    _patch_build_llm(mocker, [ChatResponse(content="not valid json")])
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Do the task.",
            arguments={},
            tool_specs=[],
            context={},
        )
    assert "no_submit_call" in str(exc_info.value.error_details or {})


@pytest.mark.asyncio
async def test_run_step_raises_when_submit_reserved_as_mcp_tool_name():
    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Do the task.",
            arguments={},
            tool_specs=[{"name": "_submit"}],
            context={},
        )
    assert exc_info.value.error_details.get("code") == "reserved_tool_name"


@pytest.mark.asyncio
async def test_run_step_raises_when_empty_submit(mocker):
    _patch_build_llm(
        mocker,
        [ChatResponse(tool_calls=[ToolCall(name="_submit", args={}, id="1")])],
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Do the task.",
            arguments={},
            tool_specs=[],
            context={},
        )
    assert "empty_submit_result" in str(exc_info.value.error_details or {})


@pytest.mark.asyncio
async def test_run_step_records_after_reason_step_when_output_schema_fails(mocker):
    """Reasoning hook runs with transcript even when _submit payload fails output_schema."""
    _patch_build_llm(
        mocker,
        [
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"wrong": "shape"}}, id="1")]
            )
        ],
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )
    mock_after = AsyncMock()
    mock_registry = MagicMock()
    mock_registry.tools.on_allowlist_passed = AsyncMock()
    mock_registry.adapter.after_reason_step = mock_after
    mocker.patch("workers.adapters.langchain.get_registry", return_value=mock_registry)
    scope = MagicMock()
    mocker.patch(
        "workers.adapters.langchain.execution_scope_from_injection",
        return_value=scope,
    )
    mocker.patch("workers.adapters.langchain.db_conn_from_injection", return_value=None)

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    output_schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }
    with pytest.raises(ExecutionStepError):
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Do the task.",
            arguments={},
            tool_specs=[],
            context={"execution_scope": scope},
            output_schema=output_schema,
        )
    mock_registry.adapter.after_reason_step.assert_called_once()
    hook_kwargs = mock_registry.adapter.after_reason_step.call_args.kwargs
    assert hook_kwargs.get("output_validation_failed") is True
    assert hook_kwargs.get("submit_payload") == {"wrong": "shape"}
    hook_messages = hook_kwargs.get("messages")
    assert hook_messages is not None
    assert len(hook_messages) >= 2


@pytest.mark.asyncio
async def test_run_step_raises_when_output_schema_validation_fails(mocker):
    _patch_build_llm(
        mocker,
        [
            ChatResponse(
                tool_calls=[ToolCall(name="_submit", args={"result": {"wrong": "shape"}}, id="1")]
            )
        ],
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    output_schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }
    with pytest.raises(ExecutionStepError) as exc_info:
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Do the task.",
            arguments={},
            tool_specs=[],
            context={},
            output_schema=output_schema,
        )
    assert (
        "validation" in str(exc_info.value.error_details or {}).lower()
        or "output_schema" in str(exc_info.value).lower()
    )


@pytest.mark.asyncio
async def test_run_step_raises_when_tool_message_indicates_failure(mocker):
    mock_tool = MagicMock()
    mock_tool.name = "some_tool"
    mock_tool.ainvoke = AsyncMock(return_value="MCP error: connection refused")

    _patch_build_llm(
        mocker,
        [ChatResponse(tool_calls=[ToolCall(name="some_tool", args={}, id="1")])],
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[mock_tool],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    with pytest.raises(ExecutionStepError):
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Hi",
            arguments={},
            tool_specs=[{"name": "some_tool"}],
            context={},
        )


@pytest.mark.asyncio
async def test_run_step_raises_when_tool_call_not_in_allowlist(mocker):
    _patch_build_llm(
        mocker,
        [ChatResponse(tool_calls=[ToolCall(name="disallowed_tool", args={}, id="1")])],
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        await adapter.run_step(
            system_prompt="You are helpful.",
            prompt_template="Hi",
            arguments={},
            tool_specs=[{"name": "allowed_tool"}],
            context={},
        )
    assert "allowlist" in str(exc_info.value).lower() or "disallowed" in str(exc_info.value).lower()
    assert exc_info.value.tool == "disallowed_tool"
    assert "disallowed_tools" in (exc_info.value.error_details or {})
    assert "allowed_tools" in (exc_info.value.error_details or {})


@pytest.mark.asyncio
async def test_run_commit_invokes_single_tool_and_returns_parsed_json(mocker):
    mock_tool = MagicMock()
    mock_tool.name = "write_row"
    mock_tool.ainvoke = AsyncMock(return_value='{"inserted": 1}')

    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[mock_tool],
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    result = await adapter.run_commit(
        arguments={"id": "x"},
        tool_specs=[{"name": "write_row"}],
        context={},
        output_schema=None,
    )
    assert result.output == {"data": {"inserted": 1}}
    mock_tool.ainvoke.assert_called_once()
    call_kw = mock_tool.ainvoke.call_args[0][0]
    assert call_kw == {"id": "x"}


@pytest.mark.asyncio
async def test_run_step_passes_max_turns_to_react_loop(mocker):
    mock_loop = mocker.patch(
        "workers.adapters.langchain.run_react_loop",
        new_callable=AsyncMock,
        return_value=mocker.MagicMock(submit_payload={"summary": "ok"}),
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        return_value=[],
    )
    _patch_build_llm(mocker, [ChatResponse(content="", tool_calls=[])])

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    await adapter.run_step(
        system_prompt="sys",
        prompt_template="Hello",
        arguments={},
        tool_specs=[],
        context={},
        max_turns=7,
    )
    assert mock_loop.await_args is not None
    assert mock_loop.await_args.kwargs["max_turns"] == 7


@pytest.mark.asyncio
async def test_run_compensation_single_tool_invokes_mcp_once_no_llm(mocker):
    mock_tool = MagicMock()
    mock_tool.name = "undo"
    mock_tool.ainvoke = AsyncMock(return_value='{"status": "compensated"}')

    async def _build_tools(*, worker_def, tool_specs, exit_stack, context, resource_specs=None):
        return [mock_tool]

    build_llm = mocker.patch("workers.adapters.langchain.build_llm")

    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        side_effect=_build_tools,
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    result = await adapter.run_compensation(
        compensation_prompt="Undo the step.",
        original_input={"claim_id": "c1"},
        step_output={"summary": "did something"},
        failure_reason=None,
        context_snapshot={},
        tool_specs=[{"name": "undo"}],
        context={},
    )
    assert result.output.get("rollback_status") == "completed"
    assert result.output.get("compensation_mode") == "single_tool"
    assert result.output.get("data", {}).get("status") == "compensated"
    assert len(result.output.get("tool_results", [])) == 1
    build_llm.assert_not_called()
    mock_tool.ainvoke.assert_called_once()
    assert mock_tool.ainvoke.call_args[0][0] == {"claim_id": "c1"}


@pytest.mark.asyncio
async def test_run_compensation_single_tool_clips_large_recorded_result(mocker, monkeypatch):
    monkeypatch.delenv("WARDEN_REACT_TOOL_MESSAGE_LIMIT", raising=False)
    large_inner = {"status": "compensated", "detail": "x" * 9000}
    mock_tool = MagicMock()
    mock_tool.name = "undo"
    mock_tool.ainvoke = AsyncMock(return_value=json.dumps(large_inner))

    async def _build_tools(*, worker_def, tool_specs, exit_stack, context, resource_specs=None):
        return [mock_tool]

    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        side_effect=_build_tools,
    )
    mocker.patch("workers.adapters.langchain.build_llm")

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    result = await adapter.run_compensation(
        compensation_prompt="Undo the step.",
        original_input={"claim_id": "c1"},
        step_output={"summary": "did something"},
        failure_reason=None,
        context_snapshot={},
        tool_specs=[{"name": "undo"}],
        context={},
    )
    recorded = result.output.get("tool_results", [])[0]["result"]
    assert len(recorded) <= 8000
    assert result.output.get("data") == large_inner


@pytest.mark.asyncio
async def test_run_compensation_multi_tool_uses_llm_loop(mocker):
    mock_tool_a = MagicMock()
    mock_tool_a.name = "a"
    mock_tool_a.ainvoke = AsyncMock(return_value="ok")
    mock_tool_b = MagicMock()
    mock_tool_b.name = "b"
    mock_tool_b.ainvoke = AsyncMock(return_value="ok")

    async def _build_tools(*, worker_def, tool_specs, exit_stack, context, resource_specs=None):
        return [mock_tool_a, mock_tool_b]

    _patch_build_llm(mocker, [ChatResponse(content='{"rollback": "done"}')])

    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        side_effect=_build_tools,
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    result = await adapter.run_compensation(
        compensation_prompt="Rollback.",
        original_input={},
        step_output={},
        failure_reason=None,
        context_snapshot={},
        tool_specs=[{"name": "a"}, {"name": "b"}],
        context={},
    )
    assert result.output.get("rollback") == "done"


@pytest.mark.asyncio
async def test_run_step_passes_resource_specs_to_build_tools(mocker):
    captured: dict[str, object] = {}

    async def _capture_build_tools(**kwargs):
        captured.update(kwargs)
        return []

    _patch_build_llm(
        mocker,
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        name="_submit",
                        args={"result": {"summary": "Done."}},
                        id="1",
                    )
                ]
            )
        ],
    )
    mocker.patch(
        "workers.adapters.langchain.build_tools_for_worker",
        new_callable=AsyncMock,
        side_effect=_capture_build_tools,
    )

    adapter = LangChainAdapter(
        worker_definition=_make_worker_def(),
        secret=_make_secret(),
    )
    resource_specs = [{"uri": "file:///policies/fraud-v3.md"}]
    await adapter.run_step(
        system_prompt="You are helpful.",
        prompt_template="Do the task.",
        arguments={},
        tool_specs=[],
        resource_specs=resource_specs,
        context={},
    )
    assert captured.get("resource_specs") == resource_specs
