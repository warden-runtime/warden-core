"""
Agent adapter: MCP tools, ChatModelPort, and native ReAct loop (_submit for reason steps).
Single-tool compensation uses deterministic MCP (run_commit); multi-tool uses the same ReAct loop.
"""

import json
import logging
import time
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any, cast

from common.agent_adapter import (
    AgentAdapterMode,
    AgentAdapterPort,
    CompensationResult,
    ExecutionStepError,
    StepResult,
)
from common.compensation_context import (
    COMPENSATION_METADATA_KEY,
    merge_compensation_tool_arguments,
)
from common.error_details import build_step_error_details
from common.execution_timing import WorkerTimingAccumulator, elapsed_ms
from common.execution_usage import WorkerUsageAccumulator
from common.governance import admit_and_validate, validate_against_schema
from common.llm import ChatMessage, ToolProtocol
from common.models import ProviderSecret, WorkerDefinition
from common.plugins.context import db_conn_from_injection, execution_scope_from_injection
from common.plugins.registry import get_registry
from common.resource_specs import ResourceSpec
from common.schemas.saga import DEFAULT_MAX_TURNS
from common.step_facts import StepFactsExtractionError, extract_step_facts
from common.step_output import business_data_from_step_output, wrap_step_output_data
from common.tool_results import clip_tool_text_for_llm, resolve_tool_message_limit
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from workers.adapters.react_loop import (
    ReactLoopResult,
    parse_compensation_output,
    run_react_loop,
)
from workers.adapters.simple_schema import resolve_effective_schema
from workers.llm import build_llm
from workers.llm.structured import invoke_structured_output
from workers.resource_runtime import READ_RESOURCE_TOOL_NAME
from workers.tools import build_tools_for_worker
from workers.utils import resolve_input

logger = logging.getLogger(__name__)


def _timing_acc_from_context(context: dict[str, Any] | None) -> WorkerTimingAccumulator | None:
    if not context:
        return None
    raw = context.get("timing")
    return raw if isinstance(raw, WorkerTimingAccumulator) else None


def _usage_acc_from_context(context: dict[str, Any] | None) -> WorkerUsageAccumulator | None:
    if not context:
        return None
    raw = context.get("usage")
    return raw if isinstance(raw, WorkerUsageAccumulator) else None


def _react_log_preview_len(context: dict[str, Any]) -> int | None:
    """Optional per-command transcript verbosity from injection context."""
    raw = context.get("react_log_preview_len")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _commit_error_details(
    *,
    scope: Any,
    base: dict[str, Any],
    tool: str,
) -> dict[str, Any]:
    """Attach saga correlation ids for commit failures (ProcessedCommand idempotency is separate)."""
    details = {**base, "tool": tool}
    if scope is not None:
        details["idempotency_key"] = scope.idempotency_key
        details["saga_trace_id"] = scope.trace_id
        details["step_span_id"] = scope.step_span_id
    return details


class _SubmitArgs(BaseModel):
    """Schema for _submit tool: single result dict (flexible structure)."""

    result: dict[str, Any] = Field(
        default_factory=dict, description="Final structured result for the step."
    )


def _build_submit_tool() -> StructuredTool:
    """Virtual _submit tool for bind_tools schema; execution is handled in react_loop."""

    def _submit_impl(result: dict[str, Any]) -> str:
        return "Submitted"

    return StructuredTool.from_function(
        func=_submit_impl,
        name="_submit",
        description="Call exactly once when the task is complete with the final structured result (e.g. summary and any required keys). No other tool after it.",
        args_schema=_SubmitArgs,
    )


def _validate_submit_payload(
    payload: dict[str, Any] | None,
    *,
    output_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    if payload is None:
        raise ExecutionStepError(
            "Agent did not call _submit with a result. Step output must be submitted via _submit.",
            error_details={"error": "no_submit_call"},
        )
    if payload == {}:
        raise ExecutionStepError(
            "Agent called _submit with empty result. Step output must include the required structure (e.g. summary, events).",
            error_details={"error": "empty_submit_result"},
        )
    if output_schema:
        try:
            return admit_and_validate(payload, output_schema, "step output (_submit)")
        except Exception as e:
            logger.exception("Step output schema validation failed: %s", e)
            raise ExecutionStepError(
                str(e),
                error_details={"error": str(e), "validation": "output_schema"},
            ) from e
    return payload


def _validate_structured_payload(
    payload: dict[str, Any] | None,
    *,
    output_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    if payload is None or payload == {}:
        message = "Structured step output must be a non-empty JSON object."
        raise ExecutionStepError(
            message,
            error_details=build_step_error_details(
                code="empty_structured_result",
                message=message,
                error="empty_structured_result",
            ),
        )
    if output_schema:
        try:
            return admit_and_validate(payload, output_schema, "step output (structured)")
        except Exception as e:
            logger.exception("Step output schema validation failed: %s", e)
            message = str(e)
            raise ExecutionStepError(
                message,
                error_details=build_step_error_details(
                    code="OUTPUT_SCHEMA_VALIDATION_FAILED",
                    message=message,
                    error=message,
                    validation="output_schema",
                ),
            ) from e
    return payload


def _reason_step_output_envelope(
    validated: dict[str, Any],
    *,
    tool_results: list[dict[str, Any]] | None,
    facts_extractors: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Wrap validated submit payload and attach tool-derived facts when configured."""
    envelope: dict[str, Any] = wrap_step_output_data(validated)
    if not facts_extractors:
        return envelope
    try:
        facts = extract_step_facts(tool_results, facts_extractors)
    except StepFactsExtractionError as exc:
        details = build_step_error_details(
            code=exc.code,
            message=exc.message,
            tool=exc.tool,
            field=exc.field,
        )
        if exc.tool_result_preview:
            details["tool_result_preview"] = exc.tool_result_preview
        if exc.truncation_limit is not None:
            details["truncation_limit"] = exc.truncation_limit
        raise ExecutionStepError(
            exc.message,
            tool=exc.tool,
            error_details=details,
        ) from exc
    if facts:
        envelope["facts"] = facts
    return envelope


class LangChainAdapter(AgentAdapterPort):
    """
    Agent adapter using MCP tools and ChatModelPort. Tools and MCP sessions are
    short-lived per call; do not hold connections across commands.
    """

    def __init__(
        self,
        worker_definition: WorkerDefinition,
        secret: ProviderSecret | SimpleNamespace,
        context: dict[str, Any] | None = None,
    ) -> None:
        self._worker_definition = worker_definition
        self._secret = secret
        self._context = context or {}

    async def _finalize_reason_step(
        self,
        *,
        ctx: dict[str, Any],
        system_prompt: str,
        prompt_template: str,
        final_input: Any,
        allowed_tool_names: list[str],
        transcript: list[ChatMessage],
        raw_payload: dict[str, Any] | None,
        output_schema: dict[str, Any] | None,
        tool_results: list[dict[str, Any]] | None,
        facts_extractors: list[dict[str, Any]] | None,
        validate_payload: Any,
    ) -> StepResult:
        scope = execution_scope_from_injection(ctx)
        conn = db_conn_from_injection(ctx)
        if scope is not None:
            await get_registry().tools.on_allowlist_passed(
                scope=scope,
                tool_names=allowed_tool_names,
                allowed_tool_names=allowed_tool_names,
                message_count=len(transcript),
                worker_definition=ctx.get("worker_definition"),
                conn=conn,
            )

        audit_payload = raw_payload if raw_payload is not None else {}
        step_error: ExecutionStepError | None = None
        output: dict[str, Any] | None = None
        try:
            validated = validate_payload(raw_payload, output_schema=output_schema)
            output = _reason_step_output_envelope(
                validated,
                tool_results=tool_results,
                facts_extractors=facts_extractors,
            )
            audit_payload = validated
        except ExecutionStepError as exc:
            step_error = exc

        if scope is not None:
            await get_registry().adapter.after_reason_step(
                messages=transcript,
                scope=scope,
                result=StepResult(output=output or {"data": audit_payload}),
                conn=conn,
                worker_definition=self._worker_definition,
                system_prompt=system_prompt,
                prompt_template=prompt_template,
                rendered_input=final_input,
                allowed_tool_names=allowed_tool_names,
                submit_payload=audit_payload,
                output_validation_failed=step_error is not None,
            )

        if step_error is not None:
            raise step_error
        return StepResult(output=output or {})

    async def _run_simple_step(
        self,
        *,
        system_prompt: str,
        prompt_template: str,
        final_input: Any,
        output_schema: dict[str, Any] | None,
        ctx: dict[str, Any],
        timing_acc: WorkerTimingAccumulator | None,
        usage_acc: WorkerUsageAccumulator | None,
        max_step_tokens: int | None,
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec],
    ) -> StepResult:
        if tool_specs or resource_specs:
            raise ExecutionStepError(
                "simple agent-adapter cannot run with MCP tools or resources",
                error_details={"code": "simple_adapter_tool_conflict"},
            )
        schema = resolve_effective_schema(output_schema)
        if timing_acc is not None:
            timing_acc.start("adapter_setup")
        llm = build_llm(
            provider=self._worker_definition.model_provider,
            model_name=self._worker_definition.model_name,
            api_key=self._secret.api_key,
        )
        initial_messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="human", content=json.dumps(final_input, default=str)),
        ]
        if timing_acc is not None:
            timing_acc.stop("adapter_setup", bucket="setup_ms")
        try:
            validated = await invoke_structured_output(
                llm,
                initial_messages,
                schema,
                timing_acc=timing_acc,
                usage_acc=usage_acc,
                max_step_tokens=max_step_tokens,
            )
        except ExecutionStepError:
            raise
        except Exception as e:
            logger.exception("Structured simple step failed: %s", e)
            raise ExecutionStepError(
                str(e),
                error_details={"error": str(e), "phase": "structured_invoke"},
            ) from e

        transcript = initial_messages + [
            ChatMessage(role="assistant", content=json.dumps(validated, ensure_ascii=False)),
        ]
        return await self._finalize_reason_step(
            ctx=ctx,
            system_prompt=system_prompt,
            prompt_template=prompt_template,
            final_input=final_input,
            allowed_tool_names=[],
            transcript=transcript,
            raw_payload=validated,
            output_schema=output_schema,
            tool_results=None,
            facts_extractors=None,
            validate_payload=_validate_structured_payload,
        )

    async def run_step(
        self,
        *,
        system_prompt: str,
        prompt_template: str,
        arguments: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec] | None = None,
        context: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        max_turns: int | None = None,
        max_step_tokens: int | None = None,
        facts_extractors: list[dict[str, Any]] | None = None,
        agent_adapter: AgentAdapterMode = "react",
    ) -> StepResult:
        ctx = context or self._context
        timing_acc = _timing_acc_from_context(ctx)
        usage_acc = _usage_acc_from_context(ctx)
        turn_budget = max_turns if max_turns is not None else DEFAULT_MAX_TURNS
        resources = resource_specs or []
        allowed_tool_names = [
            name for t in (tool_specs or []) if isinstance(name := t.get("name"), str)
        ]
        template_context = {
            **arguments,
            "allowed_tools": allowed_tool_names + (["_submit"] if agent_adapter == "react" else []),
        }
        final_input = resolve_input(template_structure=prompt_template, context=template_context)
        logger.info("Resolved prompt template: %s", final_input)

        if agent_adapter == "simple":
            return await self._run_simple_step(
                system_prompt=system_prompt,
                prompt_template=prompt_template,
                final_input=final_input,
                output_schema=output_schema,
                ctx=ctx,
                timing_acc=timing_acc,
                usage_acc=usage_acc,
                max_step_tokens=max_step_tokens,
                tool_specs=tool_specs or [],
                resource_specs=resources,
            )

        if resources:
            allowed_tool_names = allowed_tool_names + [READ_RESOURCE_TOOL_NAME]
        if "_submit" in allowed_tool_names:
            raise ExecutionStepError(
                "MCP tool name '_submit' is reserved for step completion; rename the tool in tool_specs.",
                error_details={"code": "reserved_tool_name", "tool": "_submit"},
            )
        template_context = {
            **arguments,
            "allowed_tools": allowed_tool_names + ["_submit"],
        }
        final_input = resolve_input(template_structure=prompt_template, context=template_context)
        logger.info("Resolved prompt template: %s", final_input)

        if timing_acc is not None:
            timing_acc.start("adapter_setup")
        async with AsyncExitStack() as stack:
            mcp_tools = await build_tools_for_worker(
                worker_def=self._worker_definition,
                tool_specs=tool_specs,
                exit_stack=stack,
                context=ctx,
                resource_specs=resources or None,
            )
            bind_tools = mcp_tools + [_build_submit_tool()]
            llm = build_llm(
                provider=self._worker_definition.model_provider,
                model_name=self._worker_definition.model_name,
                api_key=self._secret.api_key,
            )
            llm_with_tools = llm.bind_tools(cast("list[ToolProtocol]", bind_tools))
            initial_messages = [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="human", content=json.dumps(final_input, default=str)),
            ]
            if timing_acc is not None:
                timing_acc.stop("adapter_setup", bucket="setup_ms")
            try:
                loop_result = await run_react_loop(
                    llm=llm_with_tools,
                    initial_messages=initial_messages,
                    mcp_tools=mcp_tools,
                    allowed_tool_names=allowed_tool_names,
                    completion_mode="submit",
                    max_turns=turn_budget,
                    log_preview_len=_react_log_preview_len(ctx),
                    timing_acc=timing_acc,
                    usage_acc=usage_acc,
                    max_step_tokens=max_step_tokens,
                )
            except ExecutionStepError:
                raise
            except Exception as e:
                logger.exception("ReAct loop failed: %s", e)
                raise ExecutionStepError(
                    str(e),
                    error_details={"error": str(e), "phase": "agent_invoke"},
                ) from e

        return await self._finalize_reason_step(
            ctx=ctx,
            system_prompt=system_prompt,
            prompt_template=prompt_template,
            final_input=final_input,
            allowed_tool_names=allowed_tool_names,
            transcript=loop_result.transcript,
            raw_payload=loop_result.submit_payload,
            output_schema=output_schema,
            tool_results=loop_result.tool_results,
            facts_extractors=facts_extractors,
            validate_payload=_validate_submit_payload,
        )

    async def run_commit(
        self,
        *,
        arguments: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec] | None = None,
        context: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> StepResult:
        ctx = context or self._context
        timing_acc = _timing_acc_from_context(ctx)
        if len(tool_specs) != 1:
            raise ExecutionStepError(
                f"run_commit requires exactly one tool; got {len(tool_specs)}",
                error_details={"code": "commit_tool_count", "got": len(tool_specs)},
            )
        tool_name = (tool_specs[0].get("name") or "").strip()
        if timing_acc is not None:
            timing_acc.start("commit_setup")
        async with AsyncExitStack() as stack:
            tools = await build_tools_for_worker(
                worker_def=self._worker_definition,
                tool_specs=tool_specs,
                exit_stack=stack,
                context=ctx,
                resource_specs=resource_specs or None,
            )
            commit_tools = [tool for tool in tools if tool.name == tool_name]
            if len(commit_tools) != 1:
                raise ExecutionStepError(
                    "Commit step could not load exactly one MCP tool",
                    error_details={
                        "code": "commit_tool_load",
                        "loaded": len(commit_tools),
                        "allow": [tool_name],
                    },
                )
            tool = commit_tools[0]
            clean_args = {k: v for k, v in arguments.items() if v is not None}
            if timing_acc is not None:
                timing_acc.stop("commit_setup", bucket="setup_ms")
            scope = execution_scope_from_injection(ctx)
            if scope is not None:
                logger.info(
                    "run_commit invoking %s (namespace=%s trace=%s step=%s idempotency_key=%s)",
                    tool.name,
                    scope.namespace,
                    scope.trace_id,
                    scope.step_span_id,
                    scope.idempotency_key,
                )
            try:
                tool_start = time.perf_counter() if timing_acc is not None else None
                result_text = await tool.ainvoke(clean_args)
                if timing_acc is not None and tool_start is not None:
                    timing_acc.add_ms("tool_ms", elapsed_ms(tool_start))
            except Exception as e:
                logger.exception("run_commit tool %s failed: %s", tool.name, e)
                raise ExecutionStepError(
                    str(e),
                    tool=tool.name,
                    error_details=_commit_error_details(
                        scope=scope,
                        base={"error": str(e)},
                        tool=tool.name,
                    ),
                ) from e
            tool_name = tool.name

        if isinstance(result_text, str):
            try:
                result_parsed = json.loads(result_text)
            except json.JSONDecodeError:
                result_parsed = result_text
        else:
            result_parsed = result_text

        if not isinstance(result_parsed, dict):
            raise ExecutionStepError(
                "Commit tool must return a JSON object for output.data",
                error_details={"code": "commit_output_not_object", "tool": tool_name},
            )

        if output_schema:
            try:
                validate_against_schema(result_parsed, output_schema, "commit step output (data)")
            except Exception as e:
                logger.exception("Commit output schema validation failed: %s", e)
                raise ExecutionStepError(
                    str(e),
                    error_details={"error": str(e), "validation": "output_schema"},
                ) from e

        return StepResult(output=wrap_step_output_data(result_parsed))

    async def _run_compensation_react(
        self,
        *,
        system_instruction: str,
        final_input: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        original_input: dict[str, Any],
        ctx: dict[str, Any],
        resource_specs: list[ResourceSpec] | None = None,
        idempotency_key: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> ReactLoopResult:
        resources = resource_specs or []
        timing_acc = _timing_acc_from_context(ctx)
        usage_acc = _usage_acc_from_context(ctx)
        if timing_acc is not None:
            timing_acc.start("comp_react_setup")
        async with AsyncExitStack() as stack:
            tools = await build_tools_for_worker(
                worker_def=self._worker_definition,
                tool_specs=tool_specs,
                exit_stack=stack,
                context=ctx,
                resource_specs=resources or None,
            )
            allowed = [name for t in tool_specs if isinstance(name := t.get("name"), str)]
            if resources:
                allowed = allowed + [READ_RESOURCE_TOOL_NAME]
            llm = build_llm(
                provider=self._worker_definition.model_provider,
                model_name=self._worker_definition.model_name,
                api_key=self._secret.api_key,
            )
            llm_with_tools = llm.bind_tools(cast("list[ToolProtocol]", tools)) if tools else llm
            initial_messages = [
                ChatMessage(role="system", content=system_instruction),
                ChatMessage(role="human", content=json.dumps(final_input, default=str)),
            ]
            if timing_acc is not None:
                timing_acc.stop("comp_react_setup", bucket="setup_ms")
            return await run_react_loop(
                llm=llm_with_tools,
                initial_messages=initial_messages,
                mcp_tools=tools,
                allowed_tool_names=allowed,
                completion_mode="assistant_json",
                max_turns=max_turns,
                merge_tool_args=lambda llm_args, resolved: merge_compensation_tool_arguments(
                    llm_args,
                    resolved,
                    idempotency_key=idempotency_key,
                ),
                merge_context=original_input,
                log_preview_len=_react_log_preview_len(ctx),
                timing_acc=timing_acc,
                usage_acc=usage_acc,
            )

    async def _run_single_tool_compensation(
        self,
        *,
        fenced_input: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec] | None,
        context: dict[str, Any] | None,
        output_schema: dict[str, Any] | None,
        tool_name: str,
    ) -> CompensationResult:
        step_result = await self.run_commit(
            arguments=fenced_input,
            tool_specs=tool_specs,
            resource_specs=resource_specs,
            context=context,
            output_schema=output_schema,
        )
        inner = business_data_from_step_output(step_result.output) or {}
        logger.info(
            "Compensation (single-tool, deterministic): invoked %s once for rollback.",
            tool_name or "?",
        )
        serialized = json.dumps(inner, default=str)
        clip_limit = resolve_tool_message_limit() or 0
        recorded = (
            clip_tool_text_for_llm(serialized, limit=clip_limit) if clip_limit else serialized
        )
        return CompensationResult(
            output={
                "rollback_status": "completed",
                "compensation_mode": "single_tool",
                "data": inner,
                "tool_results": [
                    {
                        "tool": tool_name or "?",
                        "result": recorded,
                    }
                ],
            }
        )

    async def run_compensation(
        self,
        *,
        compensation_prompt: str,
        original_input: dict[str, Any],
        step_output: dict[str, Any] | None,
        failure_reason: dict[str, Any] | None,
        context_snapshot: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec] | None = None,
        context: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        idempotency_key: str | None = None,
        max_turns: int | None = None,
    ) -> CompensationResult:
        ctx = context or self._context
        turn_budget = max_turns if max_turns is not None else DEFAULT_MAX_TURNS
        effective_system_prompt = system_prompt or self._worker_definition.system_prompt
        fenced_input = merge_compensation_tool_arguments(
            None,
            original_input,
            idempotency_key=idempotency_key,
        )
        if len(tool_specs) == 1:
            spec0 = tool_specs[0]
            out_schema = spec0.get("output_schema") if isinstance(spec0, dict) else None
            tool_name = (spec0.get("name") or "").strip() if isinstance(spec0, dict) else ""
            return await self._run_single_tool_compensation(
                fenced_input=fenced_input,
                tool_specs=tool_specs,
                resource_specs=resource_specs,
                context=context,
                output_schema=out_schema,
                tool_name=tool_name,
            )

        rollback_meta = context_snapshot.get(COMPENSATION_METADATA_KEY)
        system_instruction = (
            f"{compensation_prompt}\n\n"
            f"CONTEXT: The task that failed was defined as: '{effective_system_prompt}'"
        )
        final_input = {
            "original_input": fenced_input,
            "step_output": step_output,
            "failure_reason": failure_reason,
            "context": context_snapshot,
            COMPENSATION_METADATA_KEY: rollback_meta,
        }
        loop_result = await self._run_compensation_react(
            system_instruction=system_instruction,
            final_input=final_input,
            tool_specs=tool_specs,
            original_input=fenced_input,
            ctx=ctx,
            resource_specs=resource_specs,
            idempotency_key=idempotency_key,
            max_turns=turn_budget,
        )
        output_payload = parse_compensation_output(loop_result)
        return CompensationResult(output=output_payload)
