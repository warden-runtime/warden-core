"""
Native ReAct loop on ChatModelPort: tool rounds and _submit completion.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from common.execution_timing import WorkerTimingAccumulator
    from common.execution_usage import WorkerUsageAccumulator

from common.agent_adapter import ExecutionStepError
from common.error_details import build_step_error_details
from common.execution_timing import elapsed_ms
from common.execution_usage import enforce_step_token_budget
from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolCall
from common.tool_failure import (
    annotate_recoverable_tool_output,
    format_tool_invoke_exception,
    tool_invoke_exception_is_infrastructure,
)
from common.tool_results import clip_tool_text_for_llm, tool_message_limit_from_env
from common.utils import (
    coerce_llm_json_from_schema,
    format_exception_chain,
    tool_call_args_to_dict,
    unwrap_execution_step_error,
)
from workers.adapters import state_utils
from workers.adapters.react_memory import (
    CalibratedEstimator,
    compress_if_needed,
    context_headroom_from_env,
    context_limit_from_env,
    memory_compression_enabled_from_env,
    serialize_for_estimate,
)
from workers.adapters.react_otel import (
    mark_llm_response,
    mark_memory_compression,
    mark_tool_output,
    react_llm_span,
    react_tool_span,
)
from workers.tools import get_warden_tool_input_schema, get_warden_tool_mcp_name

logger = logging.getLogger(__name__)
_transcript_logger = logging.getLogger("warden.react.transcript")
SUBMIT_TOOL_NAME = "_submit"
_DEFAULT_PREVIEW_LEN = 500
_TOOL_ERROR_PREVIEW_LEN = 500
_MAX_LAST_TOOL_ERRORS = 5
_DEFAULT_SUBMIT_TEXT_RETRIES = 1
_SUBMIT_TEXT_EXIT_NUDGE = (
    "[SYSTEM]: Your previous message was plain text without a tool call. "
    "This step requires calling the `_submit` tool now with the full result object. "
    "Do not summarize or explain in chat — invoke `_submit` immediately."
)
_SUBMIT_MUST_BE_ALONE_MESSAGE = (
    "Error: `_submit` must be the only tool call in its turn. "
    "Other tools in this batch were executed; review their results, then call "
    "`_submit` alone with the final payload."
)


def submit_text_retries_from_env() -> int:
    """Resolve ``WARDEN_REACT_SUBMIT_TEXT_RETRIES``; default 1 soft recovery on text-only exit."""
    raw = os.environ.get("WARDEN_REACT_SUBMIT_TEXT_RETRIES", str(_DEFAULT_SUBMIT_TEXT_RETRIES))
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_SUBMIT_TEXT_RETRIES


@dataclass(frozen=True)
class _TurnContinue:
    """Continue the ReAct loop after tool rounds or a submit text-exit soft retry."""

    used_submit_text_recovery: bool = False


def _content_preview_len() -> int:
    raw = os.environ.get("WARDEN_REACT_LOG_PREVIEW_LEN", str(_DEFAULT_PREVIEW_LEN))
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_PREVIEW_LEN


def _resolve_log_preview_len(override: int | None) -> int:
    """Env default unless injection context supplies react_log_preview_len (0 = no truncation)."""
    if override is not None:
        return max(0, override)
    return _content_preview_len()


def _llm_tool_content(output: str, *, tool_message_limit: int | None) -> str:
    if tool_message_limit is None:
        return output
    return clip_tool_text_for_llm(output, limit=tool_message_limit)


@dataclass(frozen=True)
class ReactLoopResult:
    """Outcome of a bounded ReAct loop."""

    transcript: list[ChatMessage]
    submit_payload: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] | None = None


def _ensure_tool_call_id(tool_call: ToolCall) -> ToolCall:
    if tool_call.id:
        return tool_call
    return ToolCall(name=tool_call.name, args=tool_call.args, id=str(uuid.uuid4()))


def _submit_payload_from_call(tool_call: ToolCall) -> dict[str, Any]:
    args = tool_call_args_to_dict(tool_call.args)
    if isinstance(args, dict) and "result" in args:
        result = args["result"]
        return result if isinstance(result, dict) else {}
    return args if isinstance(args, dict) else {}


def _check_allowlist_tool_name(
    name: str,
    allowed_tool_names: Sequence[str],
    *,
    allow_submit: bool,
) -> None:
    allowed = set(allowed_tool_names)
    if allow_submit:
        allowed.add(SUBMIT_TOOL_NAME)
    if name and name not in allowed:
        msg_text = (
            f"Tool {name!r} not in allowlist. Allowed: {', '.join(sorted(allowed)) or '(none)'}."
        )
        logger.error("Step (governance): %s", msg_text)
        raise ExecutionStepError(
            msg_text,
            tool=name,
            error_details=build_step_error_details(
                code="TOOL_NOT_ALLOWED",
                message=msg_text,
                tool=name,
                disallowed_tools=[name],
                allowed_tools=sorted(allowed),
            ),
        )


def _log_transcript(
    messages: list[ChatMessage],
    *,
    log_preview_len: int | None = None,
) -> None:
    """Emit full ReAct transcript at DEBUG (dedicated logger); not INFO noise."""
    preview_len = _resolve_log_preview_len(log_preview_len)
    _transcript_logger.debug("ReAct transcript (%d messages)", len(messages))
    for i, msg in enumerate(messages):
        content = msg.content or ""
        if preview_len and len(content) > preview_len:
            content = content[:preview_len] + "..."
        if msg.tool_calls:
            names_args = [(tc.name, tc.args) for tc in msg.tool_calls]
            _transcript_logger.debug(
                "ReAct message %d [%s] tool_calls=%s",
                i + 1,
                msg.role,
                names_args,
            )
        else:
            _transcript_logger.debug(
                "ReAct message %d [%s] content=%s",
                i + 1,
                msg.role,
                content,
            )


def _log_react_summary(
    *,
    outcome: str,
    turns_used: int,
    message_count: int,
) -> None:
    logger.info(
        "ReAct completed outcome=%s turns=%d messages=%d",
        outcome,
        turns_used,
        message_count,
    )


def _tool_not_found_result(*, tool_name: str, strict_errors: bool) -> str:
    output = f"Error: Tool {tool_name} not found."
    if strict_errors:
        raise ExecutionStepError(
            output,
            tool=tool_name,
            error_details=build_step_error_details(
                code="TOOL_NOT_FOUND",
                message=output,
                tool=tool_name,
            ),
        )
    return output


def _resolve_mcp_tool_args(
    *,
    tool_call: ToolCall,
    selected: Any,
    merge_tool_args: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    | None,
    merge_context: dict[str, Any],
) -> dict[str, Any]:
    llm_args = tool_call_args_to_dict(tool_call.args)
    schema = get_warden_tool_input_schema(selected)
    resolved = (
        merge_tool_args(llm_args, merge_context, schema)
        if merge_tool_args is not None
        else llm_args
    )
    if schema:
        return coerce_llm_json_from_schema(resolved, schema)
    return resolved


async def _call_mcp_tool(selected: Any, resolved: dict[str, Any]) -> str:
    """Invoke a LangChain/MCP tool, preserving args omitted from LLM args_schema.

    Prefer the raw coroutine so saga-wins / bind overlays survive when the
    LLM-facing ``args_schema`` omits bound keys (``ainvoke`` would drop them).
    Fall back to ``ainvoke`` for test doubles / tools without a real coroutine.
    """
    coro = getattr(selected, "coroutine", None)
    if callable(coro):
        maybe = coro(**resolved)
        if inspect.isawaitable(maybe):
            return str(await maybe)
    return str(await selected.ainvoke(resolved))


async def _invoke_mcp_tool(
    *,
    tool_call: ToolCall,
    mcp_tools: Sequence[Any],
    merge_tool_args: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    | None,
    merge_context: dict[str, Any],
    strict_errors: bool,
) -> str:
    selected = next((t for t in mcp_tools if t.name == tool_call.name), None)
    if selected is None:
        return _tool_not_found_result(tool_name=tool_call.name, strict_errors=strict_errors)

    resolved = _resolve_mcp_tool_args(
        tool_call=tool_call,
        selected=selected,
        merge_tool_args=merge_tool_args,
        merge_context=merge_context,
    )
    try:
        return await _call_mcp_tool(selected, resolved)
    except ExecutionStepError:
        raise
    except Exception as e:
        return _tool_invoke_exception_result(
            tool_call=tool_call, exc=e, strict_errors=strict_errors
        )


def _tool_invoke_exception_result(
    *,
    tool_call: ToolCall,
    exc: Exception,
    strict_errors: bool,
) -> str:
    nested = unwrap_execution_step_error(exc)
    if nested is not None:
        raise nested from exc
    detail = format_exception_chain(exc)
    logger.exception("Tool %s failed: %s", tool_call.name, exc)
    if strict_errors and tool_invoke_exception_is_infrastructure(exc):
        raise ExecutionStepError(
            detail,
            tool=tool_call.name,
            error_details=build_step_error_details(
                code="TOOL_INVOKE_FAILED",
                message=detail,
                tool=tool_call.name,
            ),
        ) from exc
    return format_tool_invoke_exception(exc)


def _handle_tool_output_content(
    content: str,
    *,
    tool_name: str,
    strict_errors: bool,
) -> None:
    if not state_utils.tool_output_indicates_failure(content):
        return
    # Recoverable mismatches (e.g. search_replace old_text not found): feed the tool
    # result into the transcript so the model can re-read and retry. Hard failures
    # (MCP transport, invalid args) still abort the step in submit mode.
    if state_utils.tool_output_is_recoverable(content):
        logger.warning(
            "Recoverable tool error fed to ReAct transcript (%s): %s",
            tool_name or "?",
            content[:500],
        )
        return
    logger.error("Tool returned error output in state: %s", content[:500])
    if strict_errors:
        message = content[:1000]
        raise ExecutionStepError(
            message,
            tool=tool_name or None,
            error_details=build_step_error_details(
                code="TOOL_OUTPUT_ERROR",
                message=message,
                tool=tool_name or None,
                error=content[:2000],
            ),
        )


def _mcp_fact_tool_name(tool_call: ToolCall, mcp_tools: Sequence[Any]) -> str:
    """Original MCP id for facts/tool_results; fall back to the LLM wire name."""
    selected = next((t for t in mcp_tools if t.name == tool_call.name), None)
    if selected is None:
        return tool_call.name
    return get_warden_tool_mcp_name(selected) or tool_call.name


def _append_tool_role_message(
    messages: list[ChatMessage],
    *,
    tool_call: ToolCall,
    content: str,
    tool_message_limit: int | None,
) -> None:
    messages.append(
        ChatMessage(
            role="tool",
            content=_llm_tool_content(content, tool_message_limit=tool_message_limit),
            tool_call_id=tool_call.id,
            name=tool_call.name,
        )
    )


def _record_submit_must_be_alone(
    *,
    messages: list[ChatMessage],
    tool_call: ToolCall,
    tool_results: list[dict[str, Any]],
    tool_message_limit: int | None,
) -> None:
    """Reject `_submit` that shares a turn with other tools; keep the loop running."""
    recorded = _SUBMIT_MUST_BE_ALONE_MESSAGE
    tool_results.append({"tool": SUBMIT_TOOL_NAME, "result": recorded})
    _append_tool_role_message(
        messages,
        tool_call=tool_call,
        content=recorded,
        tool_message_limit=tool_message_limit,
    )


async def _run_one_mcp_tool_call(
    *,
    tool_call: ToolCall,
    messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    merge_tool_args: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    | None,
    merge_context: dict[str, Any],
    tool_results: list[dict[str, Any]],
    strict_errors: bool,
    timing_acc: WorkerTimingAccumulator | None,
    tool_message_limit: int | None,
    turn_index: int,
) -> None:
    tool_start = time.perf_counter() if timing_acc is not None else None
    with react_tool_span(tool_call=tool_call, turn_index=turn_index) as tool_span:
        output = await _invoke_mcp_tool(
            tool_call=tool_call,
            mcp_tools=mcp_tools,
            merge_tool_args=merge_tool_args,
            merge_context=merge_context,
            strict_errors=strict_errors,
        )
        mark_tool_output(tool_span, output)
    if timing_acc is not None and tool_start is not None:
        timing_acc.add_ms("tool_ms", elapsed_ms(tool_start))
    _handle_tool_output_content(
        output,
        tool_name=tool_call.name,
        strict_errors=strict_errors,
    )
    # Soft mismatches / apply_patch rejects: keep raw for classification above; annotate
    # the transcript copy so the model gets a one-line recovery hint.
    recorded = annotate_recoverable_tool_output(output)
    tool_results.append(
        {
            "tool": _mcp_fact_tool_name(tool_call, mcp_tools),
            # Full payload for facts/JSONPath; do not truncate execution memory here.
            "result": recorded,
        },
    )
    _append_tool_role_message(
        messages,
        tool_call=tool_call,
        content=recorded,
        tool_message_limit=tool_message_limit,
    )


async def _execute_tool_batch(
    *,
    tool_calls: list[ToolCall],
    messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    merge_tool_args: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    | None,
    merge_context: dict[str, Any],
    tool_results: list[dict[str, Any]],
    allow_submit: bool,
    strict_errors: bool,
    timing_acc: WorkerTimingAccumulator | None,
    tool_message_limit: int | None,
    turn_index: int,
) -> None:
    """Run one assistant tool batch; reject mixed ``_submit`` calls, execute the rest."""
    if allow_submit and any(tc.name == SUBMIT_TOOL_NAME for tc in tool_calls):
        logger.warning(
            "ReAct mixed _submit batch deferred (%d tool call(s)); executing non-submit tools",
            len(tool_calls),
        )
    for tool_call in tool_calls:
        if allow_submit and tool_call.name == SUBMIT_TOOL_NAME:
            _record_submit_must_be_alone(
                messages=messages,
                tool_call=tool_call,
                tool_results=tool_results,
                tool_message_limit=tool_message_limit,
            )
            continue
        await _run_one_mcp_tool_call(
            tool_call=tool_call,
            messages=messages,
            mcp_tools=mcp_tools,
            merge_tool_args=merge_tool_args,
            merge_context=merge_context,
            tool_results=tool_results,
            strict_errors=strict_errors,
            timing_acc=timing_acc,
            tool_message_limit=tool_message_limit,
            turn_index=turn_index,
        )


async def _process_tool_calls(
    *,
    response: ChatResponse,
    messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    allowed_tool_names: Sequence[str],
    merge_tool_args: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    | None,
    merge_context: dict[str, Any],
    tool_results: list[dict[str, Any]],
    timing_acc: WorkerTimingAccumulator | None = None,
    tool_message_limit: int | None = None,
    turn_index: int = 0,
) -> dict[str, Any] | None:
    """Append assistant message, run tool calls; return submit payload if `_submit` alone completes.

    ``_submit`` is accepted only as a singleton tool batch. Mixed batches run the
    non-submit tools (in emission order), feed a rejection tool message for each
    ``_submit``, and continue the ReAct loop so the model can observe results.
    """
    tool_calls = [_ensure_tool_call_id(tc) for tc in response.tool_calls]
    messages.append(
        ChatMessage(
            role="assistant",
            content=response.content or "",
            tool_calls=tool_calls,
        )
    )
    for tool_call in tool_calls:
        _check_allowlist_tool_name(
            tool_call.name,
            allowed_tool_names,
            allow_submit=True,
        )
    if len(tool_calls) == 1 and tool_calls[0].name == SUBMIT_TOOL_NAME:
        return _submit_payload_from_call(tool_calls[0])
    await _execute_tool_batch(
        tool_calls=tool_calls,
        messages=messages,
        mcp_tools=mcp_tools,
        merge_tool_args=merge_tool_args,
        merge_context=merge_context,
        tool_results=tool_results,
        allow_submit=True,
        strict_errors=True,
        timing_acc=timing_acc,
        tool_message_limit=tool_message_limit,
        turn_index=turn_index,
    )
    return None


def _collect_last_tool_errors(tool_results: list[dict[str, Any]]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for entry in tool_results:
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool")
        raw = entry.get("result")
        if not isinstance(tool, str) or not isinstance(raw, str):
            continue
        if not state_utils.tool_output_indicates_failure(raw):
            continue
        errors.append({"tool": tool, "preview": raw[:_TOOL_ERROR_PREVIEW_LEN]})
    return errors[-_MAX_LAST_TOOL_ERRORS:]


def _raise_no_submit(
    *,
    reason: Literal["model_text_exit", "max_turns_exceeded"],
    turns_used: int,
    max_turns: int,
    tool_results: list[dict[str, Any]],
    assistant_content: str | None = None,
) -> NoReturn:
    message = "Agent did not call _submit with a result. Step output must be submitted via _submit."
    extra: dict[str, Any] = {
        "reason": reason,
        "turns_used": turns_used,
        "max_turns": max_turns,
        "error": "no_submit_call",
    }
    if reason == "model_text_exit" and isinstance(assistant_content, str):
        stripped = assistant_content.strip()
        if stripped:
            extra["last_assistant_content"] = stripped[:_TOOL_ERROR_PREVIEW_LEN]
    last_tool_errors = _collect_last_tool_errors(tool_results)
    if last_tool_errors:
        extra["last_tool_errors"] = last_tool_errors
    raise ExecutionStepError(
        message,
        error_details=build_step_error_details(
            code="no_submit_call",
            message=message,
            **extra,
        ),
    )


async def _react_loop_turn(
    *,
    llm: ChatModelPort,
    messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    allowed_tool_names: Sequence[str],
    merge_tool_args: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    | None,
    merge_context: dict[str, Any],
    tool_results: list[dict[str, Any]],
    log_preview_len: int | None,
    timing_acc: WorkerTimingAccumulator | None,
    usage_acc: WorkerUsageAccumulator | None,
    max_step_tokens: int | None,
    turns_used: int,
    max_turns: int,
    tool_message_limit: int | None = None,
    estimator: CalibratedEstimator | None = None,
    context_limit: int | None = None,
    context_headroom: float | None = None,
    allow_submit_text_recovery: bool = False,
) -> ReactLoopResult | _TurnContinue:
    memory_stats = None
    if estimator is not None:
        compressed, memory_stats = compress_if_needed(
            messages,
            max_turns=max_turns,
            context_limit=context_limit,
            estimator=estimator,
            tool_redact_limit=tool_message_limit,
            headroom=context_headroom,
        )
        messages[:] = compressed
        if usage_acc is not None and memory_stats is not None:
            usage_acc.add_memory_stats(memory_stats)

    llm_start = time.perf_counter() if timing_acc is not None else None
    with react_llm_span(turn_index=turns_used, message_count=len(messages)) as llm_span:
        if memory_stats is not None:
            mark_memory_compression(llm_span, memory_stats)
        response = await llm.ainvoke(messages)
        mark_llm_response(llm_span, response)
    if timing_acc is not None and llm_start is not None:
        timing_acc.add_ms("llm_ms", elapsed_ms(llm_start))
    if estimator is not None and response.usage and response.usage.prompt_tokens > 0:
        estimator.calibrate(serialize_for_estimate(messages), response.usage.prompt_tokens)
    if usage_acc is not None:
        usage_acc.add(response.usage)
        enforce_step_token_budget(usage_acc, max_step_tokens)
    if response.tool_calls:
        submit_payload = await _process_tool_calls(
            response=response,
            messages=messages,
            mcp_tools=mcp_tools,
            allowed_tool_names=allowed_tool_names,
            merge_tool_args=merge_tool_args,
            merge_context=merge_context,
            tool_results=tool_results,
            timing_acc=timing_acc,
            tool_message_limit=tool_message_limit,
            turn_index=turns_used,
        )
        if submit_payload is not None:
            _log_transcript(messages, log_preview_len=log_preview_len)
            _log_react_summary(
                outcome="submit",
                turns_used=turns_used,
                message_count=len(messages),
            )
            return ReactLoopResult(
                transcript=messages,
                submit_payload=submit_payload,
                tool_results=tool_results or None,
            )
        return _TurnContinue()

    if allow_submit_text_recovery:
        messages.append(ChatMessage(role="assistant", content=response.content or ""))
        messages.append(ChatMessage(role="human", content=_SUBMIT_TEXT_EXIT_NUDGE))
        logger.info(
            "ReAct submit soft-retry: model_text_exit; injected _submit nudge (turn=%d/%d)",
            turns_used,
            max_turns,
        )
        return _TurnContinue(used_submit_text_recovery=True)
    _raise_no_submit(
        reason="model_text_exit",
        turns_used=turns_used,
        max_turns=max_turns,
        tool_results=tool_results,
        assistant_content=response.content,
    )


async def run_react_loop(
    *,
    llm: ChatModelPort,
    initial_messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    allowed_tool_names: Sequence[str],
    max_turns: int,
    merge_tool_args: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    | None = None,
    merge_context: dict[str, Any] | None = None,
    log_preview_len: int | None = None,
    timing_acc: WorkerTimingAccumulator | None = None,
    usage_acc: WorkerUsageAccumulator | None = None,
    max_step_tokens: int | None = None,
) -> ReactLoopResult:
    """
    Run a bounded ReAct loop: LLM turns, MCP tool execution, then ``_submit``.

    Args:
        llm: Chat model (typically with tools bound via bind_tools).
        initial_messages: Starting transcript (system + human).
        mcp_tools: MCP StructuredTool instances (not including virtual _submit).
        allowed_tool_names: Names from step tool_specs (excludes _submit).
        max_turns: Maximum LLM invocations (text-exit soft retry may add at most
            ``WARDEN_REACT_SUBMIT_TEXT_RETRIES`` bonus call(s) when prose exit hits the last turn).
        merge_tool_args: Optional merger ``(llm_args, merge_context, input_schema) -> args``
            applied before schema coercion (e.g. saga-wins ``tools.bind`` overlay).
        merge_context: Second argument to merge_tool_args (e.g. bound ``with`` values).
        log_preview_len: Optional transcript log truncation override (from injection context);
            ``0`` disables truncation. Falls back to ``WARDEN_REACT_LOG_PREVIEW_LEN``.
        timing_acc: Optional worker timing accumulator (``llm_ms`` / ``tool_ms``).
        usage_acc: Optional worker usage accumulator (provider token totals).
        max_step_tokens: Optional accumulated total_tokens budget; None means unlimited.

    Returns:
        ReactLoopResult with transcript and ``_submit`` payload.

    Raises:
        ExecutionStepError: Governance, allowlist, tool failure, no ``_submit``, or token budget.
    """
    messages = list(initial_messages)
    tool_results: list[dict[str, Any]] = []
    ctx = merge_context if merge_context is not None else {}
    tool_message_limit = tool_message_limit_from_env()
    compression_on = memory_compression_enabled_from_env()
    estimator = CalibratedEstimator() if compression_on else None
    context_limit = context_limit_from_env() if compression_on else None
    context_headroom = context_headroom_from_env() if compression_on else None
    submit_text_recoveries_left = submit_text_retries_from_env()
    if compression_on:
        logger.debug(
            "ReAct memory compression enabled context_limit=%s headroom=%s",
            context_limit,
            context_headroom,
        )
    else:
        logger.debug("ReAct memory compression disabled")

    # Soft text-exit recovery may grant one bonus LLM call past max_turns when the
    # prose exit happens on the final scheduled turn (still capped by recoveries_left).
    effective_max_turns = max_turns
    turns_used = 0
    while turns_used < effective_max_turns:
        turns_used += 1
        turn_result = await _react_loop_turn(
            llm=llm,
            messages=messages,
            mcp_tools=mcp_tools,
            allowed_tool_names=allowed_tool_names,
            merge_tool_args=merge_tool_args,
            merge_context=ctx,
            tool_results=tool_results,
            log_preview_len=log_preview_len,
            timing_acc=timing_acc,
            usage_acc=usage_acc,
            max_step_tokens=max_step_tokens,
            turns_used=turns_used,
            max_turns=max_turns,
            tool_message_limit=tool_message_limit,
            estimator=estimator,
            context_limit=context_limit,
            context_headroom=context_headroom,
            allow_submit_text_recovery=submit_text_recoveries_left > 0,
        )
        if isinstance(turn_result, ReactLoopResult):
            return turn_result
        if turn_result.used_submit_text_recovery:
            submit_text_recoveries_left -= 1
            if turns_used >= effective_max_turns:
                effective_max_turns = turns_used + 1
        # else: tool round(s) without _submit — continue

    _raise_no_submit(
        reason="max_turns_exceeded",
        turns_used=turns_used,
        max_turns=max_turns,
        tool_results=tool_results,
    )
