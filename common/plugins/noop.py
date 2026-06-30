"""NoOp hook and default messaging factory implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.messaging.postgres import PostgresQueueConsumer, PostgresQueueProducer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from common.messaging.protocols import MessageQueueConsumer, MessageQueueProducer


class NoOpEngineLifecycleHooks:
    async def on_ingest_deduplicated(self, **kwargs: Any) -> None:
        return None

    async def on_steps_skipped_summary(self, **kwargs: Any) -> None:
        return None

    async def on_saga_transition(self, **kwargs: Any) -> None:
        return None

    async def on_step_transition(self, **kwargs: Any) -> None:
        return None

    async def on_saga_created(self, **kwargs: Any) -> None:
        return None

    async def on_step_created(self, **kwargs: Any) -> None:
        return None

    async def on_step_scheduled(self, **kwargs: Any) -> None:
        return None

    async def on_step_started(self, **kwargs: Any) -> None:
        return None

    async def on_compensation_scheduled(self, **kwargs: Any) -> None:
        return None

    async def on_hitl_review_requested(self, **kwargs: Any) -> None:
        return None

    async def on_hitl_approved(self, **kwargs: Any) -> None:
        return None

    async def on_hitl_rejected(self, **kwargs: Any) -> None:
        return None

    async def on_hitl_decision_queued(self, **kwargs: Any) -> None:
        return None

    async def on_hitl_retry_queued(self, **kwargs: Any) -> None:
        return None

    async def on_hitl_retry_requested(self, **kwargs: Any) -> None:
        return None

    async def on_operator_recovery_requested(self, **kwargs: Any) -> None:
        return None

    async def on_hitl_expired(self, **kwargs: Any) -> None:
        return None

    async def on_reaper_zombie_detected(self, **kwargs: Any) -> None:
        return None

    async def on_reaper_timeout_enforced(self, **kwargs: Any) -> None:
        return None

    async def on_reaper_race_skipped(self, **kwargs: Any) -> None:
        return None

    async def on_manifest_registered(self, **kwargs: Any) -> None:
        return None


class NoOpPolicyGateHooks:
    def get_required_modules(self) -> list[str]:
        return []

    async def on_evaluated(self, **kwargs: Any) -> None:
        return None

    async def on_denied(self, **kwargs: Any) -> None:
        return None

    async def on_errored(self, **kwargs: Any) -> None:
        return None


class NoOpWorkerLifecycleHooks:
    async def on_command_claimed(self, **kwargs: Any) -> None:
        return None

    async def on_definition_snapshot(self, **kwargs: Any) -> None:
        return None

    async def on_execution_started(self, **kwargs: Any) -> None:
        return None

    async def on_execution_completed(self, **kwargs: Any) -> None:
        return None

    async def on_execution_failed(self, **kwargs: Any) -> None:
        return None

    async def on_result_emitted(self, **kwargs: Any) -> None:
        return None

    async def on_command_rejected(self, **kwargs: Any) -> None:
        return None

    async def on_reasoning_invocation(self, **kwargs: Any) -> None:
        return None


class NoOpToolLifecycleHooks:
    def tool_output_indicates_failure(self, output: str) -> bool:
        from common.tool_failure import default_tool_output_indicates_failure

        return default_tool_output_indicates_failure(output)

    async def on_allowlist_passed(self, **kwargs: Any) -> None:
        return None

    async def on_mcp_source_attempted(self, **kwargs: Any) -> None:
        return None

    async def on_discovered(self, **kwargs: Any) -> None:
        return None

    async def on_loaded(self, **kwargs: Any) -> None:
        return None

    async def on_load_failed(self, **kwargs: Any) -> None:
        return None

    async def on_call_requested(self, **kwargs: Any) -> None:
        return None

    async def on_input_validation_passed(self, **kwargs: Any) -> None:
        return None

    async def on_input_validation_failed(self, **kwargs: Any) -> None:
        return None

    async def on_execution_completed(self, **kwargs: Any) -> None:
        return None

    async def on_execution_failed(self, **kwargs: Any) -> None:
        return None

    async def on_output_validation_passed(self, **kwargs: Any) -> None:
        return None

    async def on_output_validation_failed(self, **kwargs: Any) -> None:
        return None

    async def on_resource_allowlist_loaded(self, **kwargs: Any) -> None:
        return None

    async def on_resource_read_requested(self, **kwargs: Any) -> None:
        return None

    async def on_resource_read_completed(self, **kwargs: Any) -> None:
        return None

    async def on_resource_read_failed(self, **kwargs: Any) -> None:
        return None


class NoOpAdapterHooks:
    async def after_reason_step(self, **kwargs: Any) -> None:
        return None


class NoOpHttpExtensionRegistry:
    async def mount(self, app: Any) -> None:
        return None


class NoOpCliExtensionRegistry:
    def register(self, root: Any) -> None:
        return None


class DefaultMessagingFactory:
    def create_producer(self) -> MessageQueueProducer:
        return PostgresQueueProducer()

    def create_consumer(
        self,
        topic: str,
        group_id: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        max_in_flight: int = 1,
    ) -> MessageQueueConsumer:
        return PostgresQueueConsumer(
            topic=topic,
            group_id=group_id,
            handler=handler,
            max_in_flight=max_in_flight,
        )


def default_registry_hooks() -> dict[str, Any]:
    """Build default hook instances for PluginRegistry."""
    return {
        "engine": NoOpEngineLifecycleHooks(),
        "policy": NoOpPolicyGateHooks(),
        "worker": NoOpWorkerLifecycleHooks(),
        "tools": NoOpToolLifecycleHooks(),
        "adapter": NoOpAdapterHooks(),
        "http": NoOpHttpExtensionRegistry(),
        "cli": NoOpCliExtensionRegistry(),
        "messaging": DefaultMessagingFactory(),
    }
