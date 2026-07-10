"""
Native ReAct loop on ChatModelPort: tool rounds, _submit completion, or assistant JSON (compensation).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from common.agent_adapter import ExecutionStepError
from common.error_details import build_step_error_details
from common.execution_timing import WorkerTimingAccumulator, elapsed_ms
from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolCall
from common.tool_results import clip_tool_text_for_llm, tool_message_limit_from_env
from common.utils import (
    coerce_llm_json_from_schema,
    format_exception_chain,
    tool_call_args_to_dict,
)
from workers.adapters import state_utils
from workers.tools import get_warden_tool_input_schema

logger = logging.getLogger(__name__)

CompletionMode = Literal["submit", "assistant_json"]
SUBMIT_TOOL_NAME = "_submit"
_DEFAULT_PREVIEW_LEN = 500
_TOOL_ERROR_PREVIEW_LEN = 500
_MAX_LAST_TOOL_ERRORS = 5


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
    final_content: str | None = None
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
    preview_len = _resolve_log_preview_len(log_preview_len)
    logger.info("ReAct transcript (%d messages)", len(messages))
    for i, msg in enumerate(messages):
        content = msg.content or ""
        if preview_len and len(content) > preview_len:
            content = content[:preview_len] + "..."
        if msg.tool_calls:
            names_args = [(tc.name, tc.args) for tc in msg.tool_calls]
            logger.info(
                "ReAct message %d [%s] tool_calls=%s",
                i + 1,
                msg.role,
                names_args,
            )
        else:
            logger.info("ReAct message %d [%s] content=%s", i + 1, msg.role, content)


async def _invoke_mcp_tool(
    *,
    tool_call: ToolCall,
    mcp_tools: Sequence[Any],
    merge_tool_args: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None,
    merge_context: dict[str, Any],
    strict_errors: bool,
) -> str:
    selected = next((t for t in mcp_tools if t.name == tool_call.name), None)
    if selected is None:
        output = f"Error: Tool {tool_call.name} not found."
        if strict_errors:
            raise ExecutionStepError(
                output,
                tool=tool_call.name,
                error_details=build_step_error_details(
                    code="TOOL_NOT_FOUND",
                    message=output,
                    tool=tool_call.name,
                ),
            )
        return output

    llm_args = tool_call_args_to_dict(tool_call.args)
    resolved = merge_tool_args(llm_args, merge_context) if merge_tool_args is not None else llm_args
    schema = get_warden_tool_input_schema(selected)
    if schema:
        resolved = coerce_llm_json_from_schema(resolved, schema)
    try:
        return str(await selected.ainvoke(resolved))
    except Exception as e:
        detail = format_exception_chain(e)
        logger.exception("Tool %s failed: %s", tool_call.name, e)
        if strict_errors:
            raise ExecutionStepError(
                detail,
                tool=tool_call.name,
                error_details=build_step_error_details(
                    code="TOOL_INVOKE_FAILED",
                    message=detail,
                    tool=tool_call.name,
                ),
            ) from e
        return f"Error executing tool: {detail}"


def _handle_tool_output_content(
    content: str,
    *,
    tool_name: str,
    strict_errors: bool,
) -> None:
    if not state_utils.tool_output_indicates_failure(content):
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


async def _process_tool_calls(
    *,
    response: ChatResponse,
    messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    allowed_tool_names: Sequence[str],
    completion_mode: CompletionMode,
    merge_tool_args: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None,
    merge_context: dict[str, Any],
    tool_results: list[dict[str, Any]],
    timing_acc: WorkerTimingAccumulator | None = None,
    tool_message_limit: int | None = None,
) -> dict[str, Any] | None:
    """Append assistant message, run tool calls; return submit payload if _submit completes."""
    tool_calls = [_ensure_tool_call_id(tc) for tc in response.tool_calls]
    messages.append(
        ChatMessage(
            role="assistant",
            content=response.content or "",
            tool_calls=tool_calls,
        )
    )
    allow_submit = completion_mode == "submit"
    strict_errors = completion_mode == "submit"

    for tool_call in tool_calls:
        _check_allowlist_tool_name(
            tool_call.name,
            allowed_tool_names,
            allow_submit=allow_submit,
        )
        if allow_submit and tool_call.name == SUBMIT_TOOL_NAME:
            return _submit_payload_from_call(tool_call)

        tool_start = time.perf_counter() if timing_acc is not None else None
        output = await _invoke_mcp_tool(
            tool_call=tool_call,
            mcp_tools=mcp_tools,
            merge_tool_args=merge_tool_args,
            merge_context=merge_context,
            strict_errors=strict_errors,
        )
        if timing_acc is not None and tool_start is not None:
            timing_acc.add_ms("tool_ms", elapsed_ms(tool_start))
        _handle_tool_output_content(
            output,
            tool_name=tool_call.name,
            strict_errors=strict_errors,
        )
        tool_results.append(
            {
                "tool": tool_call.name,
                # Full payload for facts/JSONPath; do not truncate execution memory here.
                "result": output,
            },
        )
        messages.append(
            ChatMessage(
                role="tool",
                content=_llm_tool_content(output, tool_message_limit=tool_message_limit),
                tool_call_id=tool_call.id,
                name=tool_call.name,
            )
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


def _raise_compensation_max_turns(
    *,
    had_tool_results: bool,
    transcript_message_count: int,
) -> NoReturn:
    """Compensation loop exhausted max turns without final JSON (and no tool rows to synthesize)."""
    raise ExecutionStepError(
        "Compensation ReAct loop exceeded max turns without final JSON.",
        error_details={
            "error": "max_turns_exceeded",
            "code": "compensation_max_turns",
            "had_tool_results": had_tool_results,
            "transcript_message_count": transcript_message_count,
        },
    )


async def _react_loop_turn(
    *,
    llm: ChatModelPort,
    messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    allowed_tool_names: Sequence[str],
    completion_mode: CompletionMode,
    merge_tool_args: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None,
    merge_context: dict[str, Any],
    tool_results: list[dict[str, Any]],
    log_preview_len: int | None,
    timing_acc: WorkerTimingAccumulator | None,
    turns_used: int,
    max_turns: int,
    tool_message_limit: int | None = None,
) -> ReactLoopResult | None:
    llm_start = time.perf_counter() if timing_acc is not None else None
    response = await llm.ainvoke(messages)
    if timing_acc is not None and llm_start is not None:
        timing_acc.add_ms("llm_ms", elapsed_ms(llm_start))
    if response.tool_calls:
        submit_payload = await _process_tool_calls(
            response=response,
            messages=messages,
            mcp_tools=mcp_tools,
            allowed_tool_names=allowed_tool_names,
            completion_mode=completion_mode,
            merge_tool_args=merge_tool_args,
            merge_context=merge_context,
            tool_results=tool_results,
            timing_acc=timing_acc,
            tool_message_limit=tool_message_limit,
        )
        if submit_payload is not None:
            _log_transcript(messages, log_preview_len=log_preview_len)
            return ReactLoopResult(
                transcript=messages,
                submit_payload=submit_payload,
                tool_results=tool_results or None,
            )
        return None

    if completion_mode == "submit":
        _raise_no_submit(
            reason="model_text_exit",
            turns_used=turns_used,
            max_turns=max_turns,
            tool_results=tool_results,
            assistant_content=response.content,
        )

    _log_transcript(messages, log_preview_len=log_preview_len)
    return ReactLoopResult(
        transcript=messages,
        final_content=response.content,
        tool_results=tool_results or None,
    )


async def run_react_loop(
    *,
    llm: ChatModelPort,
    initial_messages: list[ChatMessage],
    mcp_tools: Sequence[Any],
    allowed_tool_names: Sequence[str],
    completion_mode: CompletionMode,
    max_turns: int,
    merge_tool_args: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    merge_context: dict[str, Any] | None = None,
    log_preview_len: int | None = None,
    timing_acc: WorkerTimingAccumulator | None = None,
) -> ReactLoopResult:
    """
    Run a bounded ReAct loop: LLM turns, MCP tool execution, then submit or assistant JSON.

    Args:
        llm: Chat model (typically with tools bound via bind_tools).
        initial_messages: Starting transcript (system + human).
        mcp_tools: MCP StructuredTool instances (not including virtual _submit).
        allowed_tool_names: Names from step tool_specs (excludes _submit).
        completion_mode: ``submit`` for reason steps (_submit required); ``assistant_json`` for compensation.
        max_turns: Maximum LLM invocations.
        merge_tool_args: Optional merger for compensation tool args with engine original_input.
        merge_context: Second argument to merge_tool_args (e.g. original_input).
        log_preview_len: Optional transcript log truncation override (from injection context);
            ``0`` disables truncation. Falls back to ``WARDEN_REACT_LOG_PREVIEW_LEN``.

    Returns:
        ReactLoopResult with transcript and completion fields.

    Raises:
        ExecutionStepError: Governance, allowlist, tool failure, no _submit, or compensation max turns.
    """
    messages = list(initial_messages)
    tool_results: list[dict[str, Any]] = []
    ctx = merge_context if merge_context is not None else {}
    tool_message_limit = tool_message_limit_from_env()

    for turn_index in range(max_turns):
        turn_result = await _react_loop_turn(
            llm=llm,
            messages=messages,
            mcp_tools=mcp_tools,
            allowed_tool_names=allowed_tool_names,
            completion_mode=completion_mode,
            merge_tool_args=merge_tool_args,
            merge_context=ctx,
            tool_results=tool_results,
            log_preview_len=log_preview_len,
            timing_acc=timing_acc,
            turns_used=turn_index + 1,
            max_turns=max_turns,
            tool_message_limit=tool_message_limit,
        )
        if turn_result is not None:
            return turn_result

    if completion_mode == "submit":
        _raise_no_submit(
            reason="max_turns_exceeded",
            turns_used=max_turns,
            max_turns=max_turns,
            tool_results=tool_results,
        )

    # Only reached when every turn had tool_calls (no early prose exit). Synthesize rollback
    # from tool rows, or fail closed when the turn budget was zero/empty.
    _log_transcript(messages, log_preview_len=log_preview_len)
    if tool_results:
        logger.warning(
            "Compensation: max turns without final JSON; synthetic output from %d tool result(s).",
            len(tool_results),
        )
        return ReactLoopResult(transcript=messages, tool_results=tool_results)
    _raise_compensation_max_turns(
        had_tool_results=False,
        transcript_message_count=len(messages),
    )


def parse_compensation_output(loop_result: ReactLoopResult) -> dict[str, Any]:
    """Build compensation output dict from loop result (synthetic or parsed JSON)."""
    if loop_result.tool_results and (
        loop_result.final_content is None or not str(loop_result.final_content).strip()
    ):
        return {
            "rollback_status": "completed",
            "tool_results": loop_result.tool_results,
        }
    content = loop_result.final_content
    if content is None:
        _raise_compensation_max_turns(
            had_tool_results=bool(loop_result.tool_results),
            transcript_message_count=len(loop_result.transcript),
        )
    clean = content.strip().replace("```json", "").replace("```", "")
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        return {"raw_output": content}
    return parsed if isinstance(parsed, dict) else {"raw_output": content}
