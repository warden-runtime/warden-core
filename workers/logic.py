import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from common.agent_adapter import StepResult
from common.compensation_context import (
    DEFAULT_COMPENSATION_PROMPT,
    compensation_parameter_context,
    compensation_prompt_from_snapshot,
    effective_forward_step_output,
    system_prompt_from_snapshot,
)
from common.config import get_settings
from common.contracts import (
    CommandType,
    CompensationFailedEvent,
    DoCommitCommand,
    DoCompensationCommand,
    DoStepCommand,
    EventType,
    StepCompensatedEvent,
    StepCompletedEvent,
    StepFailedResultEvent,
    coerce_worker_command_dict,
)
from common.execution_timing import WorkerTimingAccumulator
from common.models import ProcessedCommand, ProviderSecret, WorkerDefinition
from common.outbox import emit_saga_event
from common.plugins.context import ExecutionScope
from common.plugins.registry import get_registry
from common.processed_command_claim import (
    ClaimResult,
    mark_claim_result_emitted,
    verify_claim_before_emit,
)
from common.processed_command_reap import reap_stale_claim_by_key
from common.prompts import load_prompt_content
from common.telemetry import trace_boundary
from common.topics import TOPIC_ORCHESTRATOR_EVENTS
from common.worker_ref import assert_worker_snapshot_version, require_worker_definition
from opentelemetry.propagate import inject
from pydantic import ValidationError
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction
from workers.adapter_resolver import resolve_adapter
from workers.step_context import (
    load_forward_step,
    load_saga_instance,
    merge_resource_specs,
    merge_tool_specs,
)

logger = logging.getLogger(__name__)


def _command_type_wire(command_type: Any) -> str:
    return command_type.value if hasattr(command_type, "value") else str(command_type)


def _tool_names_from_specs(tool_specs: list[dict[str, Any]] | None) -> list[str]:
    return [str(s.get("name")) for s in (tool_specs or []) if s.get("name")]


@dataclass(frozen=True)
class _HydratedExecution:
    tool_specs: list[dict[str, Any]]
    resource_specs: list[Any]
    output_schema: dict[str, Any] | None
    prompt_template: str | None
    step_output: dict[str, Any] | None
    context_snapshot: dict[str, Any] | None
    saga_vars: dict[str, Any]
    max_turns: int
    facts_extractors: list[dict[str, Any]]
    agent_adapter: str


async def _hydrate_compensation_command(
    cmd: DoCompensationCommand,
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
) -> _HydratedExecution:
    comp_step = await load_forward_step(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
    )
    forward = await load_forward_step(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=cmd.forward_step_span_id,
    )
    saga = await load_saga_instance(namespace=namespace, saga_trace_id=saga_trace_id)
    context_snapshot = compensation_parameter_context(
        saga,
        forward,
        undo_span_id=step_span_id,
        idempotency_key=cmd.idempotency_key,
    )
    return _HydratedExecution(
        tool_specs=merge_tool_specs(cmd.tool_specs, comp_step.tools_allow),
        resource_specs=merge_resource_specs(cmd.resource_specs, comp_step.resources_allow),
        output_schema=None,
        prompt_template=None,
        step_output=effective_forward_step_output(forward),
        context_snapshot=context_snapshot,
        saga_vars=dict(context_snapshot),
        max_turns=comp_step.max_turns,
        facts_extractors=[],
        agent_adapter="react",
    )


async def _hydrate_forward_command(
    cmd: DoStepCommand | DoCommitCommand,
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
) -> _HydratedExecution:
    step = await load_forward_step(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
    )
    prompt_template: str | None = None
    if isinstance(cmd, DoStepCommand):
        prompts_root = get_settings().prompts_root
        if not prompts_root or not str(prompts_root).strip():
            raise ValueError(
                "prompts_root is not configured; set PROMPTS_ROOT on the worker service."
            )
        prompt_template = await asyncio.to_thread(
            load_prompt_content,
            prompts_root,
            cmd.prompt_ref,
        )
    output_schema = step.output_schema if isinstance(step.output_schema, dict) else None
    raw_facts = step.facts_extractors if isinstance(step.facts_extractors, list) else []
    facts_extractors = [entry for entry in raw_facts if isinstance(entry, dict)]
    return _HydratedExecution(
        tool_specs=merge_tool_specs(cmd.tool_specs, step.tools_allow),
        resource_specs=merge_resource_specs(cmd.resource_specs, step.resources_allow),
        output_schema=output_schema,
        prompt_template=prompt_template,
        step_output=None,
        context_snapshot=None,
        saga_vars=dict(cmd.arguments or {}),
        max_turns=step.max_turns,
        facts_extractors=facts_extractors,
        agent_adapter=str(getattr(step, "agent_adapter", None) or "react"),
    )


async def _hydrate_worker_command(
    cmd: DoStepCommand | DoCommitCommand | DoCompensationCommand,
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
) -> _HydratedExecution:
    if isinstance(cmd, DoCompensationCommand):
        return await _hydrate_compensation_command(
            cmd,
            namespace=namespace,
            saga_trace_id=saga_trace_id,
            step_span_id=step_span_id,
        )
    return await _hydrate_forward_command(
        cmd,
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
    )


def _validation_rejection_detail(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "schema validation failed"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    msg = str(first.get("msg", "invalid"))
    summary = f"{len(errors)} error(s); first at {loc}: {msg}"
    return summary[:512]


def _build_execution_scope(
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    idempotency_key: str,
    command_type: Any,
    worker_definition: WorkerDefinition,
    trace_context: dict[str, Any],
) -> ExecutionScope:
    return ExecutionScope(
        namespace=namespace,
        trace_id=saga_trace_id,
        step_span_id=step_span_id,
        idempotency_key=idempotency_key,
        command_type=_command_type_wire(command_type),
        worker_name=worker_definition.name,
        worker_version=worker_definition.version,
        trace_context=trace_context,
    )


def _build_execution_scope_from_command(
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    idempotency_key: str,
    command_type: Any,
    worker_name: str,
    worker_version: str,
    trace_context: dict[str, Any],
) -> ExecutionScope:
    """Execution scope when worker definition could not be loaded."""
    return ExecutionScope(
        namespace=namespace,
        trace_id=saga_trace_id,
        step_span_id=step_span_id,
        idempotency_key=idempotency_key,
        command_type=_command_type_wire(command_type),
        worker_name=worker_name,
        worker_version=worker_version,
        trace_context=trace_context,
    )


async def _claim_idempotency_key(
    *,
    idempotency_key: str,
    namespace: str,
    scope: ExecutionScope,
) -> ClaimResult:
    """Insert ProcessedCommand and command-claim audit. Returns ClaimResult."""
    handler_started_at = datetime.now(UTC)
    for attempt in range(2):
        claim_token = uuid.uuid4()
        try:
            async with in_transaction() as conn:
                await ProcessedCommand.create(
                    idempotency_key=idempotency_key,
                    namespace=namespace,
                    claim_token=claim_token,
                    result_emitted=False,
                    using_db=conn,
                )
                await get_registry().worker.on_command_claimed(
                    scope=scope,
                    conn=conn,
                )
            return ClaimResult(
                claimed=True,
                claim_token=claim_token,
                handler_started_at=handler_started_at,
            )
        except IntegrityError:
            if attempt == 0 and await reap_stale_claim_by_key(idempotency_key):
                continue
            logger.info(
                "Skipping duplicate command (idempotency_key=%s) for step %s",
                idempotency_key,
                scope.step_span_id,
            )
            return ClaimResult(claimed=False)
    return ClaimResult(claimed=False)


async def _record_definition_snapshot(
    *,
    scope: ExecutionScope,
    worker_definition: WorkerDefinition,
    tool_names_requested: list[str],
) -> None:
    """Persist worker definition snapshot after hydration (winner path only)."""
    async with in_transaction() as conn:
        await get_registry().worker.on_definition_snapshot(
            scope=scope,
            worker_definition=worker_definition,
            conn=conn,
            tool_names_requested=tool_names_requested,
        )


async def _emit_step_completed(
    conn: BaseDBAsyncClient,
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    output: dict[str, Any],
    timing: dict[str, Any] | None = None,
) -> None:
    event = StepCompletedEvent(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
        output=output,
        timing=timing,
    )
    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type=EventType.STEP_COMPLETED.value,
        payload_schema=event,
        conn=conn,
    )


def _map_execution_exception_to_output(
    exc: Exception,
    *,
    generic_error_code: str,
) -> tuple[dict[str, Any], str]:
    from common.utils import format_exception_chain, unwrap_execution_step_error

    step_error = unwrap_execution_step_error(exc)
    if step_error is not None:
        output = step_error.error_details or {"error": str(step_error)}
        details_code = output.get("code") if isinstance(output, dict) else None
        error_code = (
            details_code
            if isinstance(details_code, str) and details_code
            else getattr(step_error, "tool", None) or "execution_step_error"
        )
        return output, error_code
    return {"error": format_exception_chain(exc)}, generic_error_code


async def _run_forward_command(
    *,
    run: Callable[[], Awaitable[StepResult]],
    scope: ExecutionScope,
    worker_definition: WorkerDefinition,
    idempotency_key: str,
    claim_token: uuid.UUID,
    handler_started_at: datetime,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    failure_log_prefix: str,
    generic_error_code: str,
    success_log_message: str,
    timing_acc: WorkerTimingAccumulator,
) -> None:
    try:
        result = await run()
    except Exception as e:
        logger.exception("%s failed: %s", failure_log_prefix, e)
        wire_timing = timing_acc.to_wire() or None
        output, error_code = _map_execution_exception_to_output(
            e,
            generic_error_code=generic_error_code,
        )
        await _finalize_failure(
            scope=scope,
            worker_definition=worker_definition,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            namespace=namespace,
            saga_trace_id=saga_trace_id,
            step_span_id=step_span_id,
            event_type=EventType.STEP_FAILED,
            output=output,
            error_code=error_code,
            timing=wire_timing,
        )
    else:
        wire_timing = timing_acc.to_wire() or None
        await _finalize_success(
            scope=scope,
            worker_definition=worker_definition,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            result_event_type=EventType.STEP_COMPLETED.value,
            output=result.output,
            emit=lambda active_conn: _emit_step_completed(
                active_conn,
                namespace=namespace,
                saga_trace_id=saga_trace_id,
                step_span_id=step_span_id,
                output=result.output,
                timing=wire_timing,
            ),
        )
        logger.info(success_log_message, step_span_id)


async def _run_compensation_command(
    *,
    adapter: Any,
    cmd: DoCompensationCommand,
    hydrated: _HydratedExecution,
    worker_definition: WorkerDefinition,
    injection_context: dict[str, Any],
    scope: ExecutionScope,
    idempotency_key: str,
    claim_token: uuid.UUID,
    handler_started_at: datetime,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    timing_acc: WorkerTimingAccumulator,
) -> None:
    compensation_prompt = compensation_prompt_from_snapshot(
        cmd.worker_snapshot,
        fallback=worker_definition.compensation_prompt,
        default=DEFAULT_COMPENSATION_PROMPT,
    )
    system_prompt = system_prompt_from_snapshot(
        cmd.worker_snapshot,
        fallback=worker_definition.system_prompt,
    )
    try:
        result = await adapter.run_compensation(
            compensation_prompt=compensation_prompt,
            system_prompt=system_prompt,
            original_input=cmd.original_input,
            step_output=hydrated.step_output,
            failure_reason=cmd.failure_reason,
            context_snapshot=hydrated.context_snapshot or {},
            tool_specs=hydrated.tool_specs,
            resource_specs=hydrated.resource_specs,
            context=injection_context,
            idempotency_key=cmd.idempotency_key,
            max_turns=hydrated.max_turns,
        )
    except Exception as e:
        logger.exception("Compensation failed: %s", e)
        wire_timing = timing_acc.to_wire() or None
        await _finalize_failure(
            scope=scope,
            worker_definition=worker_definition,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            namespace=namespace,
            saga_trace_id=saga_trace_id,
            step_span_id=step_span_id,
            event_type=EventType.COMPENSATION_FAILED,
            output={"error": str(e)},
            error_code="compensation_failed",
            timing=wire_timing,
        )
        logger.warning("Reported COMPENSATION_FAILED for step %s", step_span_id)
    else:
        wire_timing = timing_acc.to_wire() or None

        async def _emit_compensated(active_conn: BaseDBAsyncClient) -> None:
            await report_result(
                namespace=namespace,
                trace_id=saga_trace_id,
                span_id=step_span_id,
                event_type=EventType.STEP_COMPENSATED,
                output=result.output,
                timing=wire_timing,
                conn=active_conn,
            )

        await _finalize_success(
            scope=scope,
            worker_definition=worker_definition,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            result_event_type=EventType.STEP_COMPENSATED.value,
            output=result.output,
            emit=_emit_compensated,
        )


async def _finalize_success(
    *,
    scope: ExecutionScope,
    worker_definition: WorkerDefinition,
    idempotency_key: str,
    claim_token: uuid.UUID,
    handler_started_at: datetime,
    result_event_type: str,
    output: dict[str, Any] | None,
    emit: Callable[[BaseDBAsyncClient], Awaitable[None]],
    conn: BaseDBAsyncClient | None = None,
) -> None:
    async def _write(active_conn: BaseDBAsyncClient) -> None:
        if not await mark_claim_result_emitted(
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            conn=active_conn,
        ):
            return
        await emit(active_conn)
        await get_registry().worker.on_execution_completed(
            scope=scope,
            conn=active_conn,
            worker_definition=worker_definition,
        )
        await get_registry().worker.on_result_emitted(
            scope=scope,
            output=output,
            conn=active_conn,
            worker_definition=worker_definition,
            result_event_type=result_event_type,
        )

    if conn is not None:
        await _write(conn)
    else:
        async with in_transaction() as active_conn:
            await _write(active_conn)


async def _finalize_adapter_resolution_failure(
    *,
    scope: ExecutionScope,
    worker_definition: WorkerDefinition | None,
    idempotency_key: str,
    claim_token: uuid.UUID,
    handler_started_at: datetime,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    error: str,
) -> None:
    """Report STEP_FAILED when adapter resolution fails after idempotency claim (one transaction)."""
    async with in_transaction() as conn:
        await _finalize_failure(
            scope=scope,
            worker_definition=worker_definition,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            namespace=namespace,
            saga_trace_id=saga_trace_id,
            step_span_id=step_span_id,
            event_type=EventType.STEP_FAILED,
            output={"error": error, "code": "adapter_resolution_failed"},
            error_code="adapter_resolution_failed",
            release_claim=True,
            conn=conn,
        )


async def _finalize_worker_config_load_failure(
    *,
    cmd: DoStepCommand | DoCommitCommand | DoCompensationCommand,
    command_type: Any,
    error: str,
    trace_context: dict[str, Any],
    claim_token: uuid.UUID,
    handler_started_at: datetime,
) -> None:
    """Notify engine and audit when worker definition/secret cannot be loaded."""
    result_event = (
        EventType.COMPENSATION_FAILED
        if isinstance(cmd, DoCompensationCommand)
        else EventType.STEP_FAILED
    )
    scope = _build_execution_scope_from_command(
        namespace=str(cmd.namespace),
        saga_trace_id=cmd.saga_trace_id,
        step_span_id=cmd.step_span_id,
        idempotency_key=cmd.idempotency_key,
        command_type=command_type,
        worker_name=str(cmd.worker_name),
        worker_version=str(cmd.worker_version),
        trace_context=trace_context,
    )
    detail = error[:512]
    failure_output = {"error": detail, "code": "worker_config_load_failed"}
    raw = cmd.model_dump(mode="json")
    async with in_transaction() as conn:
        await get_registry().worker.on_command_rejected(
            scope=None,
            reason=detail,
            conn=conn,
            raw_command=raw,
            rejection_code="worker_config_load_failed",
            detail=detail,
        )
        await _finalize_failure(
            scope=scope,
            worker_definition=None,
            idempotency_key=cmd.idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            namespace=str(cmd.namespace),
            saga_trace_id=cmd.saga_trace_id,
            step_span_id=cmd.step_span_id,
            event_type=result_event,
            output=failure_output,
            error_code="worker_config_load_failed",
            release_claim=True,
            conn=conn,
        )


async def _finalize_failure(
    *,
    scope: ExecutionScope,
    worker_definition: WorkerDefinition | None,
    idempotency_key: str,
    claim_token: uuid.UUID,
    handler_started_at: datetime,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    event_type: EventType,
    output: dict[str, Any],
    error_code: str | None = None,
    release_claim: bool = True,
    timing: dict[str, Any] | None = None,
    conn: BaseDBAsyncClient | None = None,
) -> None:
    async def _write(active_conn: BaseDBAsyncClient) -> None:
        if not await verify_claim_before_emit(
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            conn=active_conn,
        ):
            return
        await report_result(
            namespace=namespace,
            trace_id=saga_trace_id,
            span_id=step_span_id,
            event_type=event_type,
            output=output,
            timing=timing,
            conn=active_conn,
        )
        await get_registry().worker.on_execution_failed(
            scope=scope,
            conn=active_conn,
            worker_definition=worker_definition,
            error_code=error_code,
        )
        await get_registry().worker.on_result_emitted(
            scope=scope,
            output=output,
            conn=active_conn,
            worker_definition=worker_definition,
            result_event_type=event_type.value,
        )
        if release_claim:
            await _release_command_claim(
                idempotency_key,
                claim_token=claim_token,
                conn=active_conn,
            )

    if conn is not None:
        await _write(conn)
    else:
        async with in_transaction() as active_conn:
            await _write(active_conn)


@dataclass(frozen=True)
class _WorkerCommandExecution:
    cmd: DoStepCommand | DoCommitCommand | DoCompensationCommand
    command_type: CommandType
    adapter: Any
    hydrated: _HydratedExecution
    worker_definition: WorkerDefinition
    injection_context: dict[str, Any]
    scope: ExecutionScope
    idempotency_key: str
    claim_token: uuid.UUID
    handler_started_at: datetime
    namespace: str
    saga_trace_id: str
    step_span_id: str
    timing_acc: WorkerTimingAccumulator


async def _prepare_worker_command_execution(
    raw_command: dict[str, Any],
) -> _WorkerCommandExecution | None:
    parsed = await _parse_worker_command(raw_command)
    if parsed is None:
        return None
    cmd, command_type = parsed

    saga_trace_id = cmd.saga_trace_id
    step_span_id = cmd.step_span_id
    namespace = str(cmd.namespace)
    worker_name = str(cmd.worker_name)
    worker_version = str(cmd.worker_version)
    idempotency_key = cmd.idempotency_key

    headers_to_pass = {
        "X-Saga-Trace-Id": saga_trace_id,
        "X-Step-Span-Id": step_span_id,
        "Idempotency-Key": idempotency_key,
        "X-Command-Type": _command_type_wire(command_type),
    }
    inject(headers_to_pass)
    trace_context = {"headers": dict(headers_to_pass)}

    scope = _build_execution_scope_from_command(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
        idempotency_key=idempotency_key,
        command_type=command_type,
        worker_name=worker_name,
        worker_version=worker_version,
        trace_context=trace_context,
    )

    claim = await _claim_idempotency_key(
        idempotency_key=idempotency_key,
        namespace=namespace,
        scope=scope,
    )
    if not claim.claimed:
        return None
    if claim.claim_token is None or claim.handler_started_at is None:
        logger.error("Claim succeeded but missing claim_token or handler_started_at")
        return None
    claim_token = claim.claim_token
    handler_started_at = claim.handler_started_at
    timing_acc = WorkerTimingAccumulator()

    timing_acc.start("hydrate")
    try:
        hydrated = await _hydrate_worker_command(
            cmd,
            namespace=namespace,
            saga_trace_id=saga_trace_id,
            step_span_id=step_span_id,
        )
    except ValueError as e:
        timing_acc.stop("hydrate", bucket="hydration_ms")
        logger.error("Command hydration failed: %s", e)
        await _finalize_worker_config_load_failure(
            cmd=cmd,
            command_type=command_type,
            error=str(e),
            trace_context=trace_context,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
        )
        return None
    timing_acc.stop("hydrate", bucket="hydration_ms")

    timing_acc.start("setup")
    try:
        assert_worker_snapshot_version(
            getattr(cmd, "worker_snapshot", None),
            expected_version=worker_version,
        )
        worker_definition, secret = await load_worker_config(worker_name, namespace, worker_version)
    except ValueError as e:
        timing_acc.stop("setup", bucket="setup_ms")
        logger.error("Worker config load failed: %s", e)
        await _finalize_worker_config_load_failure(
            cmd=cmd,
            command_type=command_type,
            error=str(e),
            trace_context=trace_context,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
        )
        return None

    scope = _build_execution_scope(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
        idempotency_key=idempotency_key,
        command_type=command_type,
        worker_definition=worker_definition,
        trace_context=trace_context,
    )

    injection_context: dict[str, Any] = {
        "headers": headers_to_pass,
        "execution_scope": scope,
        "worker_definition": worker_definition,
        "resource_specs": hydrated.resource_specs,
        "saga_vars": hydrated.saga_vars,
        "timing": timing_acc,
    }

    try:
        adapter = resolve_adapter(
            worker_definition=worker_definition,
            secret=secret,
            context=injection_context,
        )
    except Exception as e:
        timing_acc.stop("setup", bucket="setup_ms")
        logger.exception("Worker adapter resolution failed: %s", e)
        await _finalize_adapter_resolution_failure(
            scope=scope,
            worker_definition=worker_definition,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            handler_started_at=handler_started_at,
            namespace=namespace,
            saga_trace_id=saga_trace_id,
            step_span_id=step_span_id,
            error=str(e),
        )
        return None
    timing_acc.stop("setup", bucket="setup_ms")

    await _record_definition_snapshot(
        scope=scope,
        worker_definition=worker_definition,
        tool_names_requested=_tool_names_from_specs(hydrated.tool_specs),
    )

    logger.info(
        "Worker processing command (%s) for Step %s (Trace: %s)",
        _command_type_wire(command_type),
        step_span_id,
        saga_trace_id,
    )

    async with in_transaction() as conn:
        await get_registry().worker.on_execution_started(
            scope=scope,
            conn=conn,
            worker_definition=worker_definition,
        )

    return _WorkerCommandExecution(
        cmd=cmd,
        command_type=command_type,
        adapter=adapter,
        hydrated=hydrated,
        worker_definition=worker_definition,
        injection_context=injection_context,
        scope=scope,
        idempotency_key=idempotency_key,
        claim_token=claim_token,
        handler_started_at=handler_started_at,
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
        timing_acc=timing_acc,
    )


async def _dispatch_worker_command_execution(execution: _WorkerCommandExecution) -> None:
    cmd = execution.cmd
    if isinstance(cmd, DoCompensationCommand):
        await _run_compensation_command(
            adapter=execution.adapter,
            cmd=cmd,
            hydrated=execution.hydrated,
            worker_definition=execution.worker_definition,
            injection_context=execution.injection_context,
            scope=execution.scope,
            idempotency_key=execution.idempotency_key,
            claim_token=execution.claim_token,
            handler_started_at=execution.handler_started_at,
            namespace=execution.namespace,
            saga_trace_id=execution.saga_trace_id,
            step_span_id=execution.step_span_id,
            timing_acc=execution.timing_acc,
        )
        return

    if isinstance(cmd, DoStepCommand):
        prompt_template = execution.hydrated.prompt_template
        if not prompt_template:
            raise ValueError("DO_STEP hydration did not load prompt_template")
        await _run_forward_command(
            run=lambda: execution.adapter.run_step(
                system_prompt=execution.worker_definition.system_prompt,
                prompt_template=prompt_template,
                arguments=cmd.arguments,
                tool_specs=execution.hydrated.tool_specs,
                resource_specs=execution.hydrated.resource_specs,
                context=execution.injection_context,
                output_schema=execution.hydrated.output_schema,
                max_turns=execution.hydrated.max_turns,
                facts_extractors=execution.hydrated.facts_extractors or None,
                agent_adapter=execution.hydrated.agent_adapter,  # type: ignore[arg-type]
            ),
            scope=execution.scope,
            worker_definition=execution.worker_definition,
            idempotency_key=execution.idempotency_key,
            claim_token=execution.claim_token,
            handler_started_at=execution.handler_started_at,
            namespace=execution.namespace,
            saga_trace_id=execution.saga_trace_id,
            step_span_id=execution.step_span_id,
            failure_log_prefix="Step",
            generic_error_code="step_failed",
            success_log_message="Reported STEP_COMPLETED for step %s",
            timing_acc=execution.timing_acc,
        )
        return

    if isinstance(cmd, DoCommitCommand):
        await _run_forward_command(
            run=lambda: execution.adapter.run_commit(
                arguments=cmd.arguments,
                tool_specs=execution.hydrated.tool_specs,
                resource_specs=execution.hydrated.resource_specs,
                context=execution.injection_context,
                output_schema=execution.hydrated.output_schema,
            ),
            scope=execution.scope,
            worker_definition=execution.worker_definition,
            idempotency_key=execution.idempotency_key,
            claim_token=execution.claim_token,
            handler_started_at=execution.handler_started_at,
            namespace=execution.namespace,
            saga_trace_id=execution.saga_trace_id,
            step_span_id=execution.step_span_id,
            failure_log_prefix="Commit step",
            generic_error_code="commit_failed",
            success_log_message="Reported STEP_COMPLETED for commit step %s",
            timing_acc=execution.timing_acc,
        )


# Write a real step_name. Not boilerplate.
@trace_boundary(span_name_key="step_name")
async def handle_worker_command(raw_command: dict[str, Any]) -> None:
    """Main entry point for worker commands: parse, load config, run adapter, report result.

    Dispatches on type (DO_STEP, DO_COMMIT, EXECUTE_COMPENSATION). Invalid commands are
    logged and dropped (no exception). Reports outcomes via outbox
    (STEP_COMPLETED, STEP_FAILED, STEP_COMPENSATED, etc.).

    Args:
        raw_command: Command dict with type, saga_trace_id, step_span_id,
            namespace, worker_name, idempotency_key, and type-specific fields.
    """
    execution = await _prepare_worker_command_execution(raw_command)
    if execution is None:
        return
    await _dispatch_worker_command_execution(execution)


async def _release_command_claim(
    idempotency_key: str,
    *,
    claim_token: uuid.UUID | None = None,
    conn: BaseDBAsyncClient | None = None,
) -> None:
    """Remove ProcessedCommand row so redelivery can retry after a failure."""
    q = ProcessedCommand.filter(idempotency_key=idempotency_key)
    if claim_token is not None:
        q = q.filter(claim_token=claim_token)
    if conn is not None:
        q = q.using_db(conn)
    deleted = await q.delete()
    if deleted:
        logger.debug("Released claim for idempotency_key=%s to allow retry", idempotency_key)


def _env_var_for_provider(provider: str) -> str | None:
    """Return the environment variable name for the provider's API key, or None if no fallback."""
    normalized = (provider or "").strip().lower()
    if normalized in ("openai", "local"):
        return "OPENAI_API_KEY"
    if normalized == "anthropic":
        return "ANTHROPIC_API_KEY"
    return None


def _strip_non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _api_key_from_secret(secret: ProviderSecret | None) -> str | None:
    if secret is None:
        return None
    return _strip_non_empty(secret.api_key)


def _api_key_from_env(provider: str) -> str | None:
    env_var = _env_var_for_provider(provider)
    if not env_var:
        logger.debug("No env fallback for provider %r.", provider)
        return None
    raw = os.environ.get(env_var)
    present = env_var in os.environ
    logger.info(
        "Env fallback for %s: %s present=%s, non_empty=%s",
        provider,
        env_var,
        present,
        bool(_strip_non_empty(raw)),
    )
    api_key = _strip_non_empty(raw)
    if api_key:
        logger.info("Provider %s authenticated via environment fallback.", provider)
    return api_key


def _raise_missing_api_key(*, provider: str, namespace: str) -> None:
    logger.error(
        "Provider %s auth missing in namespace %s.",
        provider,
        namespace,
    )
    raise ValueError(
        f"No API key found for {provider} (Namespace: {namespace}). "
        "Add a row to provider_secrets or set the provider's API key in the environment."
    )


_COMMAND_PARSERS: dict[
    CommandType,
    type[DoStepCommand] | type[DoCommitCommand] | type[DoCompensationCommand],
] = {
    CommandType.DO_STEP: DoStepCommand,
    CommandType.DO_COMMIT: DoCommitCommand,
    CommandType.EXECUTE_COMPENSATION: DoCompensationCommand,
}


def _coerce_command_type(raw: Any) -> CommandType | None:
    if isinstance(raw, CommandType):
        return raw
    if isinstance(raw, str):
        try:
            return CommandType(raw)
        except ValueError:
            return None
    return None


async def _reject_worker_command(
    *,
    raw_command: dict[str, Any],
    reason: str,
    rejection_code: str,
    detail: str | None = None,
) -> None:
    await get_registry().worker.on_command_rejected(
        scope=None,
        reason=reason[:512],
        raw_command=raw_command,
        rejection_code=rejection_code,
        detail=(detail or reason)[:512],
    )


async def _parse_worker_command(
    raw_command: dict[str, Any],
) -> tuple[DoStepCommand | DoCommitCommand | DoCompensationCommand, CommandType] | None:
    try:
        wire = coerce_worker_command_dict(raw_command)
        command_type = _coerce_command_type(wire.get("type"))
        if command_type is None:
            logger.error("Unknown command type: %s", wire.get("type"))
            await _reject_worker_command(
                raw_command=raw_command,
                reason=f"unknown command type: {wire.get('type')!r}",
                rejection_code="unknown_command_type",
            )
            return None
        parser = _COMMAND_PARSERS[command_type]
        return parser(**wire), command_type
    except ValidationError as e:
        logger.error("Dropping invalid command (schema mismatch): %s", e)
        detail = _validation_rejection_detail(e)
        await _reject_worker_command(
            raw_command=raw_command,
            reason=detail,
            rejection_code="schema_validation_failed",
            detail=detail,
        )
        return None


async def load_worker_config(
    worker_name: str, namespace: str, worker_version: str
) -> tuple[WorkerDefinition, ProviderSecret | SimpleNamespace]:
    """Fetch WorkerDefinition and API key for the given worker identity.

    Resolves the API key from (1) ProviderSecret in the DB, or (2) the provider's
    environment variable (e.g. OPENAI_API_KEY) when no secret exists or api_key
    is empty. `provider: local` does not require a key — an empty value is passed
    through and the LLM factory supplies a placeholder for OpenAI-compatible servers.

    Args:
        worker_name: Worker definition name.
        namespace: Tenant namespace.
        worker_version: Worker definition version.

    Returns:
        Tuple of (WorkerDefinition, object with .api_key). The second element is
        either a ProviderSecret (from DB) or a SimpleNamespace(api_key=...) from env.

    Raises:
        ValueError: If WorkerDefinition not found or no API key in DB or env.
    """
    worker_definition = await require_worker_definition(
        namespace=namespace,
        name=worker_name,
        version=worker_version,
    )
    return worker_definition, await _resolve_provider_secret(
        namespace=namespace,
        worker_definition=worker_definition,
    )


async def _resolve_provider_secret(
    *,
    namespace: str,
    worker_definition: WorkerDefinition,
) -> ProviderSecret | SimpleNamespace:
    provider = (worker_definition.model_provider or "").strip().lower()
    secret = await ProviderSecret.get_or_none(
        namespace=namespace, provider=worker_definition.model_provider
    )
    api_key = _api_key_from_secret(secret) or _api_key_from_env(provider)
    if not api_key and provider not in ("local", "mock"):
        _raise_missing_api_key(provider=provider, namespace=namespace)

    if secret is not None and _api_key_from_secret(secret):
        return secret
    return SimpleNamespace(api_key=api_key or "")


async def report_result(
    namespace: str,
    trace_id: str,
    span_id: str,
    event_type: EventType,
    output: dict[str, Any],
    conn: BaseDBAsyncClient | None = None,
    timing: dict[str, Any] | None = None,
) -> None:
    """Emit step result to engine via outbox (STEP_COMPENSATED, STEP_FAILED, COMPENSATION_FAILED).

    Args:
        namespace: Tenant namespace.
        trace_id: Saga trace_id.
        span_id: Step span_id.
        event_type: One of STEP_COMPENSATED, STEP_FAILED, COMPENSATION_FAILED.
        output: Result payload (and error_details for failure events).
        conn: Optional DB connection for transactional emit (same transaction as ProcessedCommand).
    """
    if event_type == EventType.STEP_COMPENSATED:
        event = StepCompensatedEvent(
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=span_id,
            type=EventType.STEP_COMPENSATED,
            output=output,
            timing=timing,
        )
    elif event_type == EventType.STEP_FAILED:
        event = StepFailedResultEvent(
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=span_id,
            output=output,
            error_details=output,
            timing=timing,
        )
    elif event_type == EventType.COMPENSATION_FAILED:
        event = CompensationFailedEvent(
            namespace=namespace,
            saga_trace_id=trace_id,
            step_span_id=span_id,
            output=output,
            error_details=output,
            timing=timing,
        )
    else:
        logger.error("Unknown event_type %s for report_result; dropping.", event_type)
        return

    await emit_saga_event(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        event_type=event_type.value,
        payload_schema=event,
        conn=conn,
    )
    logger.info("Reported %s to engine for step %s", event_type.value, span_id)
