"""Seed saga/step rows so worker command hydration succeeds in unit tests."""

from __future__ import annotations

from common.models import SagaInstance, SagaStatus, SagaStepInstance, StepStatus


async def seed_forward_step(
    *,
    namespace: str = "default",
    saga_trace_id: str,
    step_span_id: str,
    prompt_ref: str | None = "p.j2",
    worker: str = "test-worker",
    worker_version: str = "1.0.0",
    tools_allow: list | None = None,
    resources_allow: list | None = None,
    output_schema: dict | None = None,
    output_payload: dict | None = None,
    error_details: dict | None = None,
    step_id: str = "step-1",
    step_kind: str = "reason",
) -> tuple[SagaInstance, SagaStepInstance]:
    saga = await SagaInstance.create(
        trace_id=saga_trace_id,
        namespace=namespace,
        definition_id="test-def",
        status=SagaStatus.RUNNING,
        context={"input": {}, "steps": {}},
    )
    step = await SagaStepInstance.create(
        span_id=step_span_id,
        saga_trace_id=saga_trace_id,
        namespace=namespace,
        saga=saga,
        step_id=step_id,
        step_name=step_id,
        order_index=0,
        idempotency_key=f"{saga_trace_id}-{step_id}",
        timeout_seconds=600,
        status=StepStatus.IN_PROGRESS,
        worker=worker,
        worker_version=worker_version,
        step_kind=step_kind,
        tools_allow=tools_allow or [],
        resources_allow=resources_allow or [],
        parameters_spec={},
        resolved_arguments={},
        prompt_ref=prompt_ref,
        output_schema=output_schema,
        output_payload=output_payload,
        error_details=error_details,
    )
    return saga, step


async def seed_compensation_steps(
    *,
    namespace: str = "default",
    saga_trace_id: str,
    forward_span_id: str,
    comp_span_id: str,
    worker: str = "comp-worker",
    worker_version: str = "1.0.0",
    forward_output: dict | None = None,
    tools_allow: list | None = None,
) -> tuple[SagaInstance, SagaStepInstance, SagaStepInstance]:
    saga = await SagaInstance.create(
        trace_id=saga_trace_id,
        namespace=namespace,
        definition_id="test-def",
        status=SagaStatus.COMPENSATING,
        context={"input": {}, "steps": {}},
    )
    forward = await SagaStepInstance.create(
        span_id=forward_span_id,
        saga_trace_id=saga_trace_id,
        namespace=namespace,
        saga=saga,
        step_id="fwd",
        step_name="fwd",
        order_index=0,
        idempotency_key=f"{saga_trace_id}-fwd",
        timeout_seconds=600,
        status=StepStatus.FAILED,
        worker=worker,
        worker_version=worker_version,
        step_kind="reason",
        tools_allow=[],
        resources_allow=[],
        parameters_spec={},
        resolved_arguments={},
        prompt_ref="p.j2",
        output_payload=forward_output,
        error_details={"error": "timeout"},
    )
    comp = await SagaStepInstance.create(
        span_id=comp_span_id,
        compensates_span_id=forward_span_id,
        saga_trace_id=saga_trace_id,
        namespace=namespace,
        saga=saga,
        step_id="fwd",
        step_name="fwd",
        order_index=0,
        idempotency_key=f"comp-{saga_trace_id}-{comp_span_id}",
        timeout_seconds=600,
        status=StepStatus.COMPENSATING,
        worker=worker,
        worker_version=worker_version,
        step_kind="reason",
        tools_allow=tools_allow or [],
        resources_allow=[],
        parameters_spec={},
        resolved_arguments={},
        prompt_ref=None,
    )
    return saga, forward, comp
