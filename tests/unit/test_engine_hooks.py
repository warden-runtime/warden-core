"""Engine lifecycle hook dispatch (no full saga DB)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from common.contracts import StepCompletedIngestEvent
from common.models import EventType
from common.plugins import register_engine_hooks, reset_registry
from common.plugins.registry import get_registry


@dataclass
class _CountingEngineHooks:
    ingest_dedup: int = 0
    calls: list[str] = field(default_factory=list)

    async def on_ingest_deduplicated(self, **kwargs: object) -> None:
        self.ingest_dedup += 1
        self.calls.append("ingest_dedup")

    async def on_steps_skipped_summary(self, **kwargs: object) -> None:
        self.calls.append("steps_skipped")

    async def on_saga_transition(self, **kwargs: object) -> None:
        self.calls.append("saga_transition")

    async def on_step_transition(self, **kwargs: object) -> None:
        self.calls.append("step_transition")

    async def on_saga_created(self, **kwargs: object) -> None:
        return None

    async def on_step_created(self, **kwargs: object) -> None:
        return None

    async def on_step_scheduled(self, **kwargs: object) -> None:
        return None

    async def on_step_started(self, **kwargs: object) -> None:
        return None

    async def on_compensation_scheduled(self, **kwargs: object) -> None:
        return None

    async def on_hitl_review_requested(self, **kwargs: object) -> None:
        return None

    async def on_hitl_approved(self, **kwargs: object) -> None:
        return None

    async def on_hitl_rejected(self, **kwargs: object) -> None:
        return None

    async def on_hitl_decision_queued(self, **kwargs: object) -> None:
        return None

    async def on_hitl_expired(self, **kwargs: object) -> None:
        return None

    async def on_hitl_retry_queued(self, **kwargs: object) -> None:
        return None

    async def on_hitl_retry_requested(self, **kwargs: object) -> None:
        return None

    async def on_reaper_zombie_detected(self, **kwargs: object) -> None:
        return None

    async def on_reaper_timeout_enforced(self, **kwargs: object) -> None:
        return None

    async def on_reaper_race_skipped(self, **kwargs: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolated_engine_hooks():
    reset_registry()
    yield
    reset_registry()


@pytest.mark.asyncio
async def test_notify_skipped_ingest_dispatches_hook():
    from engine.logic import _notify_skipped_ingest

    hooks = _CountingEngineHooks()
    register_engine_hooks(hooks)
    event = StepCompletedIngestEvent(
        saga_trace_id="a" * 32,
        namespace="default",
        event_type=EventType.STEP_COMPLETED,
        step_span_id="b" * 16,
        output={"data": {}},
    )
    await _notify_skipped_ingest(
        event,
        dedup_reason="terminal_step_state",
        conn=None,
    )
    assert hooks.ingest_dedup == 1
    assert get_registry().engine is hooks
