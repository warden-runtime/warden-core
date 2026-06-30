"""Worker command timing must be snapshotted after run(), not before."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from common.agent_adapter import StepResult
from common.execution_timing import WorkerTimingAccumulator
from workers.logic import _run_forward_command


@pytest.mark.asyncio
async def test_run_forward_command_emits_timing_after_run():
    timing_acc = WorkerTimingAccumulator()
    timing_acc.add_ms("hydration_ms", 1)
    timing_acc.add_ms("setup_ms", 2)
    emitted_timing: dict | None = None

    async def _run() -> StepResult:
        timing_acc.add_ms("llm_ms", 2500)
        return StepResult(output={"data": {"summary": "ok"}})

    async def _capture_emit(_conn, *, timing, **_) -> None:
        nonlocal emitted_timing
        emitted_timing = timing

    async def _fake_finalize_success(**kwargs) -> None:
        await kwargs["emit"](None)

    with (
        patch("workers.logic._finalize_success", new=AsyncMock(side_effect=_fake_finalize_success)),
        patch("workers.logic._emit_step_completed", new=AsyncMock(side_effect=_capture_emit)),
    ):
        await _run_forward_command(
            run=_run,
            scope=AsyncMock(),
            worker_definition=AsyncMock(),
            idempotency_key="idem",
            claim_token=AsyncMock(),
            handler_started_at=AsyncMock(),
            namespace="default",
            saga_trace_id="trace",
            step_span_id="span",
            failure_log_prefix="Step",
            generic_error_code="step_failed",
            success_log_message="ok %s",
            timing_acc=timing_acc,
        )

    assert emitted_timing is not None
    worker = emitted_timing.get("worker") or {}
    assert worker.get("hydration_ms") == 1
    assert worker.get("setup_ms") == 2
    assert worker.get("llm_ms") == 2500
