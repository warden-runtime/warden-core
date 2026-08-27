"""Worker hydrate loads ``tools_bind`` from the step row and passes it to ``run_step``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from common.agent_adapter import StepResult
from common.contracts import DoStepCommand
from common.models import ProviderSecret, WorkerDefinition
from tests.worker_hydration_helpers import seed_forward_step
from workers.logic import _hydrate_forward_command, handle_worker_command


def _do_step_command(
    *,
    trace_id: str,
    span_id: str,
    worker: str = "bind-test-worker",
    worker_version: str = "1.0.0",
    arguments: dict | None = None,
) -> DoStepCommand:
    return DoStepCommand(
        type="DO_STEP",
        namespace="default",
        saga_trace_id=trace_id,
        step_span_id=span_id,
        worker_name=worker,
        worker_version=worker_version,
        idempotency_key=f"{trace_id}-bind-hydrate",
        prompt_ref="p.j2",
        arguments=arguments or {"container_id": "saga-c1"},
        tool_specs=[{"name": "sandbox_exec"}],
    )


@pytest.mark.asyncio
async def test_hydrate_forward_command_loads_tools_bind_from_step_row() -> None:
    trace_id = "a" * 32
    span_id = "b" * 16
    await seed_forward_step(
        saga_trace_id=trace_id,
        step_span_id=span_id,
        worker="bind-test-worker",
        worker_version="1.0.0",
        tools_bind=["container_id"],
        prompt_ref="p.j2",
    )
    cmd = _do_step_command(trace_id=trace_id, span_id=span_id)

    hydrated = await _hydrate_forward_command(
        cmd,
        namespace="default",
        saga_trace_id=trace_id,
        step_span_id=span_id,
    )

    assert hydrated.tool_bind_keys == ["container_id"]


@pytest.mark.asyncio
async def test_hydrate_forward_command_tools_bind_defaults_empty() -> None:
    trace_id = "e" * 32
    span_id = "f" * 16
    await seed_forward_step(
        saga_trace_id=trace_id,
        step_span_id=span_id,
        worker="bind-test-worker",
        worker_version="1.0.0",
        tools_bind=[],
        prompt_ref="p.j2",
    )
    cmd = _do_step_command(trace_id=trace_id, span_id=span_id)

    hydrated = await _hydrate_forward_command(
        cmd,
        namespace="default",
        saga_trace_id=trace_id,
        step_span_id=span_id,
    )

    assert hydrated.tool_bind_keys == []


@pytest.mark.asyncio
async def test_handle_worker_command_passes_tools_bind_to_run_step(mocker) -> None:
    trace_id = "c" * 32
    span_id = "d" * 16
    await seed_forward_step(
        saga_trace_id=trace_id,
        step_span_id=span_id,
        worker="bind-test-worker",
        worker_version="1.0.0",
        tools_bind=["container_id"],
        prompt_ref="p.j2",
    )
    await WorkerDefinition.create(
        namespace="default",
        name="bind-test-worker",
        version="1.0.0",
        model_provider="openai",
        model_name="gpt-4o-mini",
        system_prompt="Bind hydration test.",
    )
    await ProviderSecret.create(
        id=uuid4(),
        namespace="default",
        provider="openai",
        api_key="sk-test",
    )

    captured: dict[str, object] = {}
    fake_adapter = MagicMock()

    async def _run_step(**kwargs: object) -> StepResult:
        captured.update(kwargs)
        return StepResult(output={"data": {"summary": "ok"}})

    fake_adapter.run_step = AsyncMock(side_effect=_run_step)
    mocker.patch("workers.logic.resolve_adapter", return_value=fake_adapter)

    cmd = _do_step_command(trace_id=trace_id, span_id=span_id)
    await handle_worker_command(cmd.model_dump(mode="json"))

    fake_adapter.run_step.assert_awaited_once()
    assert captured.get("tool_bind_keys") == ["container_id"]
    assert captured.get("arguments") == {"container_id": "saga-c1"}
