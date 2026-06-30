import uuid
from enum import StrEnum

from tortoise import fields, models


class EventType(StrEnum):
    """Events that trigger the Engine state machine."""

    SAGA_STARTED = "SAGA_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    STEP_COMPENSATED = "STEP_COMPENSATED"
    SAGA_COMPLETED = "SAGA_COMPLETED"
    SAGA_COMPENSATED = "SAGA_COMPENSATED"
    SAGA_FAILED = "SAGA_FAILED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    HUMAN_RETRY = "HUMAN_RETRY"


class StepStatus(StrEnum):
    """The lifecycle status of an individual Saga step."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    SKIPPED = "SKIPPED"
    TIMED_OUT = "TIMED_OUT"
    AWAITING_HUMAN = "AWAITING_HUMAN"


class SagaStatus(StrEnum):
    """The high-level status of the entire saga."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    COMPENSATING = "COMPENSATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"


class SagaDefinition(models.Model):
    """
    Read-only template for creating SagaInstances.
    Managed by the Control Plane.
    """

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    namespace = fields.CharField(max_length=50, default="default", db_index=True)

    name = fields.CharField(max_length=128)
    version = fields.CharField(max_length=50, default="0.0.1")
    is_active = fields.BooleanField(default=True)

    # The Blueprint: List of steps, worker configs, etc.
    body = fields.JSONField()

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "saga_definitions"
        # Enforce unique versions per name per tenant
        unique_together = (("namespace", "name", "version"),)


class SagaStepInstance(models.Model):
    """The definition of an individual Saga step."""

    span_id = fields.CharField(primary_key=True, max_length=16)
    saga_trace_id = fields.CharField(max_length=32)
    namespace = fields.CharField(max_length=50, default="default", db_index=True)
    saga = fields.ForeignKeyField(
        "models.SagaInstance", related_name="steps", on_delete=fields.CASCADE
    )

    step_id = fields.CharField(max_length=128)
    step_name = fields.CharField(max_length=128)
    order_index = fields.IntField()
    idempotency_key = fields.CharField(max_length=128, unique=True)
    started_at = fields.DatetimeField(auto_now_add=True, db_index=True)
    end_time = fields.DatetimeField(
        null=True
    )  # Set when step reaches terminal state (reaper/audit)
    timeout_seconds = fields.IntField()
    max_turns = fields.IntField(
        default=10,
        description="Max LLM invocations for reason-step ReAct and multi-tool compensation.",
    )
    agent_adapter = fields.CharField(
        max_length=32,
        default="react",
        description="Reason-step execution strategy: react (ReAct + _submit) or simple (structured single turn).",
    )
    status = fields.CharEnumField(
        StepStatus,
        default=StepStatus.PENDING,
        max_length=50,
    )

    worker = fields.CharField(max_length=255)
    worker_version = fields.CharField(max_length=50)
    step_kind = fields.CharField(max_length=32)
    tools_allow = fields.JSONField(
        default=list,
        description="List of tool spec dicts from saga YAML tools.allow (name, optional schemas).",
    )
    resources_allow = fields.JSONField(
        default=list,
        description=(
            "List of resource spec dicts from saga YAML resources.allow "
            "(uri, optional description)."
        ),
    )
    parameters_spec = fields.JSONField(
        default=dict,
        description="Step ``with`` map: argument name → {from: JSONPath} or {value: literal}.",
    )
    resolved_arguments = fields.JSONField(
        default=dict,
        description="Evaluated arguments passed to the worker after JSONPath resolution.",
    )
    prompt_ref = fields.CharField(max_length=512, null=True)
    output_payload = fields.JSONField(
        null=True,
        description="STEP_COMPLETED/FAILED worker output object; business data under output.data when set.",
    )
    error_details = fields.JSONField(
        null=True,
        description="Structured failure metadata (code, message, tool) when the step errors.",
    )
    compensation_definition = fields.JSONField(
        null=True,
        description="Resolved compensation block dict (worker, with, tools) from COMPENSATIONS_ROOT YAML.",
    )
    output_schema = fields.JSONField(
        null=True,
        description="Resolved step output JSON Schema (Draft-7 object) loaded from SCHEMAS_ROOT.",
    )
    policy_name = fields.CharField(max_length=128, null=True)
    hitl_required = fields.BooleanField(default=False)
    hitl_max_retries = fields.IntField(
        null=True,
        description="Max manual HITL retries from manifest; null means unlimited.",
    )
    hitl_retry_count = fields.IntField(
        default=0,
        description="Number of manual HITL retries already applied for this forward step.",
    )
    hitl_retry_guidance = fields.TextField(
        null=True,
        description="Default retry guidance from saga YAML; API may override per request.",
    )
    hitl_review_started_at = fields.DatetimeField(null=True)
    pending_review_payload = fields.JSONField(
        null=True,
        description="HITL review envelope shown to operators (output or arguments snapshot).",
    )
    when_cel = fields.TextField(
        null=True,
        description="Optional CEL schedule gate from saga manifest when.cel; evaluated before scheduling.",
    )
    facts_extractors = fields.JSONField(
        default=list,
        description="Tool-facts extractor specs from saga manifest facts block (reason steps).",
    )
    # When set, this row is a compensation execution for the forward step with this span_id.
    # Forward rows created at saga start have compensates_span_id NULL.
    compensates_span_id = fields.CharField(max_length=16, null=True, db_index=True)
    execution_timing = fields.JSONField(
        null=True,
        description="Merged worker/engine timing buckets (ms); never merged into saga context.",
    )
    pending_engine_timing = fields.JSONField(
        null=True,
        description="In-flight engine timing staging and dispatch perf_counter anchor.",
    )

    class Meta:
        table = "saga_step_instances"


class SagaInstance(models.Model):
    """The definition of an individual Saga."""

    trace_id = fields.CharField(primary_key=True, max_length=32)
    namespace = fields.CharField(max_length=50, default="default", db_index=True)

    definition_id = fields.CharField(max_length=128)
    status = fields.CharEnumField(SagaStatus, default=SagaStatus.PENDING, max_length=50)

    context = fields.JSONField(default=dict)
    start_idempotency_key = fields.CharField(max_length=256, null=True)
    started_at = fields.DatetimeField(auto_now_add=True, db_index=True)

    class Meta:
        table = "saga_instances"
        unique_together = (
            ("namespace", "trace_id"),
            ("namespace", "start_idempotency_key"),
        )


class WorkerDefinition(models.Model):
    """Configuration for a specific AI Agent."""

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    namespace = fields.CharField(max_length=50, default="default", db_index=True)
    name = fields.CharField(max_length=128)
    version = fields.CharField(max_length=50, default="1.0.0")

    # CharFields aren't great here. Enum would be better. Gotta figure out how
    # we can populate this without restarting the container.
    model_provider = fields.CharField(max_length=64)
    model_name = fields.CharField(max_length=128)

    system_prompt = fields.TextField()
    compensation_prompt = fields.TextField(null=True)
    tool_sources = fields.JSONField(default=list)
    adapter = fields.CharField(max_length=32, default="langchain")

    # This is good, but we'll need versioning at some point.
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "worker_definitions"
        unique_together = (("namespace", "name", "version"),)


class ProviderSecret(models.Model):
    """Stores encrypted API keys for tenants. Make sure to encrypt keys at
    rest."""

    id = fields.UUIDField(primary_key=True)
    namespace = fields.CharField(max_length=50, default="default", db_index=True)

    provider = fields.CharField(max_length=64)  # e.g. openai
    api_key = fields.CharField(max_length=512)

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "provider_secrets"
        # Might want to unrestrict this later.
        unique_together = ("namespace", "provider")


class OutboxStatus(StrEnum):
    """Status of an outbox message for consumer processing."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OutboxEvent(models.Model):
    """
    Transactional outbox: single source of truth for all produced messages.
    Producers write here in the same DB transaction as domain changes.
    When idempotency_key is set, at most one row per
    (namespace, destination_topic, idempotency_key) is stored.
    """

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    namespace = fields.CharField(max_length=50, default="default", db_index=True)
    saga_trace_id = fields.CharField(max_length=32, db_index=True)
    step_span_id = fields.CharField(max_length=16)
    event_type = fields.CharField(max_length=128)
    destination_topic = fields.CharField(max_length=255, db_index=True)
    idempotency_key = fields.CharField(max_length=256, null=True)
    trace_context = fields.JSONField(
        default=dict,
        description="OpenTelemetry propagation carrier: string-keyed dict (e.g. traceparent).",
    )
    payload = fields.JSONField(
        description="Serialized command or saga event dict (JSON object) for the consumer handler.",
    )
    status = fields.CharEnumField(
        OutboxStatus, default=OutboxStatus.PENDING, max_length=32, db_index=True
    )
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "outbox_events"
        unique_together = (("namespace", "destination_topic", "idempotency_key"),)


class ProcessedCommand(models.Model):
    """Records idempotency_key of worker commands; claim-first prevents double execution.

    Row is created at start of handling (claim). On success, result_emitted is set True
    in the same transaction as the outbox emit. Duplicate delivery hits IntegrityError
    on create and is skipped. Rows with result_emitted=False and old created_at can be
    reaped to allow retry after worker crash before emit.
    """

    idempotency_key = fields.CharField(primary_key=True, max_length=256)
    namespace = fields.CharField(max_length=50, default="default", db_index=True)
    claim_token = fields.UUIDField(default=uuid.uuid4)
    result_emitted = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "processed_commands"


class ProcessedIngestEvent(models.Model):
    """Records event_dedup_key of engine ingest events already applied; re-delivery is no-op."""

    event_dedup_key = fields.CharField(primary_key=True, max_length=256)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "processed_ingest_events"


class ProcessedOperatorRecovery(models.Model):
    """HTTP idempotency snapshot for operator retry-step / retry-compensation requests."""

    dedup_key = fields.CharField(primary_key=True, max_length=256)
    request_fingerprint = fields.CharField(max_length=128)
    response_json = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "processed_operator_recoveries"
