"""Plugin hook and extension protocols (stubs; wired in later chunks)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tortoise.backends.base.client import BaseDBAsyncClient

    from common.messaging.protocols import MessageQueueConsumer, MessageQueueProducer
    from common.plugins.context import ExecutionScope
    from common.policy_gate import PolicyGateResult
    from common.schemas.policy import PolicyPhase


class EngineLifecycleHooks(Protocol):
    async def on_ingest_deduplicated(
        self,
        *,
        namespace: str,
        saga_trace_id: str,
        step_span_id: str,
        reason: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_steps_skipped_summary(
        self,
        *,
        namespace: str,
        saga_trace_id: str,
        skipped_count: int,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_saga_transition(
        self,
        *,
        saga: Any,
        from_status: str,
        to_status: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_step_transition(
        self,
        *,
        saga: Any,
        step: Any,
        from_status: str,
        to_status: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_saga_created(
        self,
        *,
        saga: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_step_created(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_step_scheduled(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_step_started(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_compensation_scheduled(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_hitl_review_requested(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_hitl_approved(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_hitl_rejected(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_hitl_decision_queued(
        self,
        *,
        saga: Any,
        step: Any,
        decision: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_hitl_retry_queued(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_hitl_retry_requested(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_operator_recovery_requested(
        self,
        *,
        saga: Any,
        step: Any,
        recovery_kind: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_hitl_expired(
        self,
        *,
        saga: Any,
        step: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_reaper_zombie_detected(
        self,
        *,
        namespace: str,
        saga_trace_id: str,
        step_span_id: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_reaper_timeout_enforced(
        self,
        *,
        namespace: str,
        saga_trace_id: str,
        step_span_id: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_reaper_race_skipped(
        self,
        *,
        namespace: str,
        saga_trace_id: str,
        step_span_id: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_manifest_registered(
        self,
        *,
        kind: str,
        blueprint: Any,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...


class PolicyGateHooks(Protocol):
    def get_required_modules(self) -> list[str]:
        """Tortoise model module paths (e.g. enterprise.models) required by this plugin."""
        ...

    async def on_evaluated(
        self,
        *,
        phase: PolicyPhase,
        binding: dict[str, Any],
        result: PolicyGateResult,
        namespace: str,
        saga_trace_id: str,
        step_span_id: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> None: ...

    async def on_denied(
        self,
        *,
        phase: PolicyPhase,
        binding: dict[str, Any],
        result: PolicyGateResult,
        namespace: str,
        saga_trace_id: str,
        step_span_id: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> None: ...

    async def on_errored(
        self,
        *,
        phase: PolicyPhase,
        binding: dict[str, Any],
        result: PolicyGateResult,
        namespace: str,
        saga_trace_id: str,
        step_span_id: str,
        conn: BaseDBAsyncClient | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> None: ...


class WorkerLifecycleHooks(Protocol):
    async def on_command_claimed(
        self,
        *,
        scope: ExecutionScope,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_definition_snapshot(
        self,
        *,
        scope: ExecutionScope,
        worker_definition: Any,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_execution_started(
        self,
        *,
        scope: ExecutionScope,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_execution_completed(
        self,
        *,
        scope: ExecutionScope,
        output: Any = None,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_execution_failed(
        self,
        *,
        scope: ExecutionScope,
        error_details: dict[str, Any] | None = None,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_result_emitted(
        self,
        *,
        scope: ExecutionScope,
        output: Any = None,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_command_rejected(
        self,
        *,
        scope: ExecutionScope | None,
        reason: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_reasoning_invocation(
        self,
        *,
        scope: ExecutionScope,
        capture: Any = None,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...


class ToolLifecycleHooks(Protocol):
    async def on_allowlist_passed(
        self,
        *,
        scope: ExecutionScope,
        tool_names: list[str],
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_mcp_source_attempted(
        self,
        *,
        scope: ExecutionScope,
        source: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_discovered(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_loaded(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_load_failed(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        error_message: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_call_requested(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        arguments: dict[str, Any],
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_input_validation_passed(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_input_validation_failed(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        error_message: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_execution_completed(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        output: Any = None,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_execution_failed(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        error_message: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_output_validation_passed(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_output_validation_failed(
        self,
        *,
        scope: ExecutionScope,
        tool_name: str,
        error_message: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_resource_allowlist_loaded(
        self,
        *,
        scope: ExecutionScope,
        resource_uris: list[str],
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_resource_read_requested(
        self,
        *,
        scope: ExecutionScope,
        resource_uri: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_resource_read_completed(
        self,
        *,
        scope: ExecutionScope,
        resource_uri: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def on_resource_read_failed(
        self,
        *,
        scope: ExecutionScope,
        resource_uri: str,
        error_message: str,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...

    def tool_output_indicates_failure(self, output: str) -> bool:
        """Return True when tool return text should be treated as a failed invocation."""
        ...


class AdapterHooks(Protocol):
    async def after_reason_step(
        self,
        *,
        messages: list[Any],
        scope: ExecutionScope,
        result: Any,
        conn: BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> None: ...


class HttpExtensionRegistry(Protocol):
    async def mount(self, app: Any) -> None: ...


class CliExtensionRegistry(Protocol):
    def register(self, root: Any) -> None: ...


class MessagingFactory(Protocol):
    def create_producer(self) -> MessageQueueProducer: ...

    def create_consumer(
        self,
        topic: str,
        group_id: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        max_in_flight: int = 1,
    ) -> MessageQueueConsumer: ...
