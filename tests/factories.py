import uuid
from typing import Any

import factory
from common.models import SagaInstance, SagaStatus, SagaStepInstance, StepStatus

TEST_COMPENSATION_DEF = {
    "worker": "test-worker",
    "worker_version": "1.0.0",
    "tools": {"allow": [{"name": "void_payment"}]},
}


_UNSET = object()


def worker_definition_body(
    *,
    name: str = "test-worker",
    namespace: str = "default",
    version: str = "1.0.0",
    provider: str = "openai",
    model_name: str = "gpt-4o",
    system_prompt: str = "Hi.",
    tool_sources: list | None = None,
    adapter: str = "langchain",
    temperature: float = 0.0,
    description: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Deploy-shaped worker manifest body for WorkerDefinition.create(body=...)."""
    body: dict[str, Any] = {
        "kind": "worker",
        "name": name,
        "namespace": namespace,
        "version": version,
        "provider": provider,
        "model_name": model_name,
        "system_prompt": system_prompt,
        "tool_sources": tool_sources if tool_sources is not None else [],
        "adapter": adapter,
        "temperature": temperature,
    }
    if description is not None:
        body["description"] = description
    body.update(overrides)
    return body


def step_definition_body(
    *,
    name: str = "test-step",
    namespace: str = "default",
    version: str = "1.0.0",
    step_kind: str = "reason",
    worker: str = "test-worker",
    worker_version: str = "1.0.0",
    title: Any = _UNSET,
    inputs: dict[str, Any] | None = None,
    prompt: Any = _UNSET,
    tools: Any = _UNSET,
    description: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Deploy-shaped step manifest body for StepDefinition.create / parse_step_blueprint.

    Pass ``prompt=None`` / ``tools=None`` / ``title=None`` explicitly to omit or null those
    fields (for validation tests). Omit the kwargs to get reason/commit defaults.
    """
    body: dict[str, Any] = {
        "kind": "step",
        "name": name,
        "namespace": namespace,
        "version": version,
        "inputs": {} if inputs is None else inputs,
        "step_kind": step_kind,
        "worker": worker,
        "worker_version": worker_version,
    }
    if title is not _UNSET:
        body["title"] = title
    if description is not None:
        body["description"] = description
    if step_kind == "reason":
        body["prompt"] = "noop.j2" if prompt is _UNSET else prompt
        if tools is not _UNSET:
            body["tools"] = tools
    else:
        body["tools"] = {"allow": [{"name": "noop_tool"}]} if tools is _UNSET else tools
        if prompt is not _UNSET:
            body["prompt"] = prompt
    body.update(overrides)
    return body


class SagaDataFactory(factory.Factory):
    class Meta:
        model = SagaInstance

    trace_id = factory.LazyFunction(lambda: uuid.uuid4().hex)
    namespace = "default"
    definition_id = "patient_intake_v1"
    status = SagaStatus.PENDING

    # FIX 1: Default to Namespaced Context (Critical for new architecture)
    # Old: {"patient_id": "123"}
    # New: {"input": {...}, "steps": {}}
    context = factory.LazyFunction(lambda: {"input": {"patient_id": "123"}, "steps": {}})


class StepDataFactory(factory.Factory):
    class Meta:
        model = SagaStepInstance

    span_id = factory.LazyFunction(lambda: uuid.uuid4().hex[:16])
    step_id = factory.Sequence(lambda n: f"step_{n}")
    step_name = factory.Sequence(lambda n: f"step_{n}")
    order_index = factory.Sequence(lambda n: n)
    forward_seq = factory.Sequence(lambda n: n)
    loop_id = None
    iteration = None
    idempotency_key = factory.LazyFunction(lambda: uuid.uuid4().hex)
    status = StepStatus.PENDING

    worker = "test-worker"
    worker_version = "1.0.0"
    step_kind = "reason"
    namespace = "default"
    parameters_spec = factory.LazyFunction(dict)
    prompt_ref = "step-1-fraud.j2"
    prompt_definition = "Frozen test prompt for {{ input }}"
    output_payload = factory.LazyFunction(dict)

    timeout_seconds = 300
    max_turns = 10
    agent_adapter = "react"
    hitl_max_retries = None
    hitl_retry_count = 0
    hitl_retry_guidance = None
    saga_trace_id = ""  # Placeholder


async def create_saga_with_steps(
    step_count=3,
    initial_context=None,
    **saga_overrides,
):
    """
    Creates a Saga + Steps in the in-memory SQLite DB.
    """
    # 1. Prepare Saga Data
    # Allow passing 'initial_context' directly for easier test setup
    if initial_context:
        saga_overrides["context"] = initial_context

    saga_stub = SagaDataFactory.stub(**saga_overrides)

    # Filter out keys that aren't in the model (Safety check)
    saga_data = {k: v for k, v in saga_stub.__dict__.items() if k in SagaInstance._meta.fields}

    saga = await SagaInstance.create(**saga_data)

    steps = []
    for i in range(step_count):
        # 2. Create Step Stub
        # We explicitly sync the IDs to ensure foreign key integrity
        step_stub = StepDataFactory.stub(
            saga=saga,
            saga_trace_id=saga.trace_id,
            namespace=saga.namespace,  # Must match parent!
            order_index=i,
            forward_seq=i,
            step_id=f"step_{i}",
            step_name=f"step_{i}",
        )

        # 3. Save to DB — only model fields (factory stub may carry extra attributes)
        field_names = set(SagaStepInstance._meta.fields_map.keys())
        step_data = {k: v for k, v in step_stub.__dict__.items() if k in field_names}
        step = await SagaStepInstance.create(**step_data)
        steps.append(step)

    return saga, steps
