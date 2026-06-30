import uuid

import factory
from common.models import SagaInstance, SagaStatus, SagaStepInstance, StepStatus

TEST_COMPENSATION_DEF = {
    "worker": "test-worker",
    "worker_version": "1.0.0",
    "tools": {"allow": [{"name": "void_payment"}]},
}


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
    idempotency_key = factory.LazyFunction(lambda: uuid.uuid4().hex)
    status = StepStatus.PENDING

    worker = "test-worker"
    worker_version = "1.0.0"
    step_kind = "reason"
    namespace = "default"
    parameters_spec = factory.LazyFunction(
        lambda: {
            "transaction_amount": {"from": "$.input.amount"},
            "merchant_id": {"from": "$.input.merchant_id"},
        }
    )
    prompt_ref = "step-1-fraud.j2"
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
            step_id=f"step_{i}",
            step_name=f"step_{i}",
        )

        # 3. Save to DB — only model fields (factory stub may carry extra attributes)
        field_names = set(SagaStepInstance._meta.fields_map.keys())
        step_data = {k: v for k, v in step_stub.__dict__.items() if k in field_names}
        step = await SagaStepInstance.create(**step_data)
        steps.append(step)

    return saga, steps
