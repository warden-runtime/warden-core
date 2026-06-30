import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.resource_specs import ResourceSpec

_CONTRACT_CONFIG = ConfigDict(populate_by_name=True, use_enum_values=True, extra="forbid")
_INGEST_CONFIG = ConfigDict(populate_by_name=True, extra="forbid")


# =========================================================
# 1. ENUMS (Shared Constants)
# =========================================================
class CommandType(StrEnum):
    DO_STEP = "DO_STEP"
    DO_COMMIT = "DO_COMMIT"
    EXECUTE_COMPENSATION = "EXECUTE_COMPENSATION"


class EventType(StrEnum):
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    STEP_COMPENSATED = "STEP_COMPENSATED"
    SAGA_COMPLETED = "SAGA_COMPLETED"
    SAGA_COMPENSATED = "SAGA_COMPENSATED"
    SAGA_FAILED = "SAGA_FAILED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


# =========================================================
# 2. BASE MODELS (The "Envelope" Fields)
# =========================================================
class BaseSagaEnvelope(BaseModel):
    """Routing identity shared by commands, worker replies, and ingest events."""

    namespace: str = Field(default="default", pattern=r"^[a-z0-9-]+$")
    saga_trace_id: str = Field(..., min_length=32, max_length=32)

    model_config = _CONTRACT_CONFIG


class BaseSagaMessage(BaseSagaEnvelope):
    """Step-scoped saga message (commands and worker → orchestrator replies)."""

    step_span_id: str = Field(..., min_length=16, max_length=16, pattern=r"^[a-f0-9]{16}$")


def coerce_worker_command_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Drop outbox envelope keys that are not part of worker command models."""
    return {k: v for k, v in data.items() if k not in ("event_type", "trace_context")}


# SagaEventPayload / outbox wire fields not on ingest models.
_INGEST_STRIP_ALWAYS = frozenset({"status", "idempotency_key"})
_INGEST_STRIP_BY_EVENT: dict[str, frozenset[str]] = {
    "SAGA_STARTED": frozenset({"output", "error_details"}),
}


def coerce_saga_ingest_dict(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize outbox wire dicts for ``SagaIngestEvent`` validation."""
    if data is None:
        return {}
    coerced = (
        {k: v for k, v in data.items() if k != "type"}
        if "event_type" in data and "type" in data
        else dict(data)
    )
    event_type = coerced.get("event_type")
    strip = set(_INGEST_STRIP_ALWAYS)
    if isinstance(event_type, str):
        strip |= set(_INGEST_STRIP_BY_EVENT.get(event_type, ()))
    if not strip:
        return coerced
    return {k: v for k, v in coerced.items() if k not in strip}


# =========================================================
# 3. COMMAND CONTRACTS (Orchestrator -> Worker)
# =========================================================
class DoStepCommand(BaseSagaMessage):
    """LLM step with tools; worker replies with STEP_COMPLETED or STEP_FAILED."""

    type: Literal[CommandType.DO_STEP] = CommandType.DO_STEP
    worker_name: str
    worker_version: str
    idempotency_key: str
    prompt_ref: str = Field(
        ...,
        description="Relative path under PROMPTS_ROOT; worker loads template content at execution.",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_specs: list[dict[str, Any]] = Field(
        default_factory=list,
        description='Slim allowlist: [{"name": "tool_name"}, ...]; full specs on SagaStepInstance.',
    )
    resource_specs: list[ResourceSpec] = Field(default_factory=list)


class DoCommitCommand(BaseSagaMessage):
    """Single governed MCP tool call; worker replies with STEP_COMPLETED or STEP_FAILED."""

    type: Literal[CommandType.DO_COMMIT] = CommandType.DO_COMMIT
    worker_name: str
    worker_version: str
    idempotency_key: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_specs: list[dict[str, Any]] = Field(
        default_factory=list,
        description='Slim allowlist: [{"name": "tool_name"}, ...]; full specs on SagaStepInstance.',
    )
    resource_specs: list[ResourceSpec] = Field(default_factory=list)


class DoCompensationCommand(BaseSagaMessage):
    """Undo a forward step; worker replies with STEP_COMPENSATED or COMPENSATION_FAILED."""

    type: Literal[CommandType.EXECUTE_COMPENSATION] = CommandType.EXECUTE_COMPENSATION
    worker_name: str
    worker_version: str
    idempotency_key: str
    forward_step_span_id: str = Field(
        ...,
        min_length=16,
        max_length=16,
        pattern=r"^[a-f0-9]{16}$",
        description="Span id of the forward step being compensated; worker hydrates output/context from DB.",
    )
    original_input: dict[str, Any] = Field(default_factory=dict)
    tool_specs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Slim allowlist from compensation definition; full specs merged from DB.",
    )
    resource_specs: list[ResourceSpec] = Field(default_factory=list)
    failure_reason: dict[str, Any] | None = None
    worker_snapshot: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Worker definition fields frozen when compensation was scheduled "
            "(prompts, model, version)."
        ),
    )


WorkerCommand = DoStepCommand | DoCommitCommand | DoCompensationCommand


# =========================================================
# 4. EVENT CONTRACTS (Worker -> Orchestrator outbox payload)
# =========================================================
class ReaperTimeoutErrorDetails(BaseModel):
    """Outbox STEP_FAILED output/error_details when the reaper enforces a step timeout."""

    error: str = "Timeout enforced by reaper"
    code: Literal["TIMEOUT"] = "TIMEOUT"
    source: Literal["reaper"] = "reaper"

    model_config = _INGEST_CONFIG


class ReaperStepTimedOutOutput(BaseModel):
    """output_payload persisted on SagaStepInstance when reaper marks TIMED_OUT."""

    error: str = "Timeout enforced by reaper"
    code: Literal["408"] = "408"

    model_config = _INGEST_CONFIG


class ReaperStepTimedOutDbError(BaseModel):
    """error_details persisted on SagaStepInstance when reaper marks TIMED_OUT."""

    source: Literal["reaper"] = "reaper"
    reason: Literal["execution_timeout"] = "execution_timeout"

    model_config = _INGEST_CONFIG


class WorkerResultEvent(BaseSagaMessage):
    """Shared worker reply fields (outbox payload before envelope merge)."""

    output: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] | None = None


class StepFailedResultEvent(WorkerResultEvent):
    """Reply from Worker when a step fails (or reaper timeout)."""

    type: Literal[EventType.STEP_FAILED] = EventType.STEP_FAILED
    error_details: dict[str, Any] | None = None


def step_failed_from_reaper_timeout(
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    timeout_error: ReaperTimeoutErrorDetails | None = None,
) -> StepFailedResultEvent:
    """Build a STEP_FAILED outbox event from the reaper timeout contract."""
    details = timeout_error or ReaperTimeoutErrorDetails()
    wire = details.model_dump(mode="json")
    return StepFailedResultEvent(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
        output=wire,
        error_details=wire,
    )


class StepCompletedEvent(WorkerResultEvent):
    """Worker finished the agentic step."""

    type: Literal[EventType.STEP_COMPLETED] = EventType.STEP_COMPLETED


class StepCompensatedEvent(WorkerResultEvent):
    """Reply from Worker when undo is finished."""

    type: Literal[EventType.STEP_COMPENSATED] = EventType.STEP_COMPENSATED


class CompensationFailedEvent(WorkerResultEvent):
    """Worker reported that a compensation step failed."""

    type: Literal[EventType.COMPENSATION_FAILED] = EventType.COMPENSATION_FAILED
    error_details: dict[str, Any] | None = None


class ReaperCompensationTimeoutErrorDetails(BaseModel):
    """Outbox COMPENSATION_FAILED error_details when the reaper enforces an undo SLA."""

    code: Literal["COMPENSATION_TIMEOUT"] = "COMPENSATION_TIMEOUT"
    error: str = "Compensation step exceeded timeout_seconds."
    source: Literal["reaper"] = "reaper"
    reason: Literal["compensation_timeout"] = "compensation_timeout"

    model_config = _INGEST_CONFIG


def compensation_failed_from_reaper_timeout(
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    timeout_error: ReaperCompensationTimeoutErrorDetails | None = None,
) -> CompensationFailedEvent:
    """Build a COMPENSATION_FAILED outbox event when an undo row exceeds its SLA."""
    details = timeout_error or ReaperCompensationTimeoutErrorDetails()
    wire = details.model_dump(mode="json")
    return CompensationFailedEvent(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
        output=wire,
        error_details=wire,
    )


WorkerEvent = (
    StepFailedResultEvent | StepCompletedEvent | StepCompensatedEvent | CompensationFailedEvent
)


# =========================================================
# 5. SAGA-LEVEL OUTBOX PAYLOAD (orchestrator → orchestrator)
# =========================================================
class SagaEventPayload(BaseSagaEnvelope):
    """Orchestrator-emitted saga/step notification stored in the outbox payload column."""

    step_span_id: str | None = Field(default=None)
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    error_details: dict[str, Any] | None = None

    @field_validator("step_span_id")
    @classmethod
    def step_span_id_none_or_hex(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not re.match(r"^[a-f0-9]{16}$", v) or len(v) != 16:
            raise ValueError("step_span_id must be 16 hex characters when present")
        return v


# =========================================================
# 6. ENGINE INGEST EVENTS (Orchestrator consumer)
# =========================================================
class BaseSagaIngestEvent(BaseSagaEnvelope):
    """Envelope for events consumed by ``process_saga_event`` (discriminated on event_type)."""

    event_type: str
    trace_context: dict[str, Any] | None = None
    step_span_id: str | None = None
    timing: dict[str, Any] | None = None

    model_config = _INGEST_CONFIG

    @field_validator("step_span_id")
    @classmethod
    def step_span_id_none_or_hex(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not re.match(r"^[a-f0-9]{16}$", v) or len(v) != 16:
            raise ValueError("step_span_id must be 16 hex characters when present")
        return v


class StepLevelSagaIngestEvent(BaseSagaIngestEvent):
    """Ingest events that require a step span id."""

    @model_validator(mode="after")
    def _require_step_span_id(self) -> Self:
        if not self.step_span_id:
            raise ValueError("step_span_id is required")
        return self


class SagaStartedEvent(BaseSagaIngestEvent):
    event_type: Literal["SAGA_STARTED"] = "SAGA_STARTED"


class StepFailedEvent(StepLevelSagaIngestEvent):
    event_type: Literal["STEP_FAILED"] = "STEP_FAILED"
    error_details: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


class StepCompensatedIngestEvent(StepLevelSagaIngestEvent):
    event_type: Literal["STEP_COMPENSATED"] = "STEP_COMPENSATED"
    output: dict[str, Any] = Field(default_factory=dict)


class StepCompletedIngestEvent(StepLevelSagaIngestEvent):
    event_type: Literal["STEP_COMPLETED"] = "STEP_COMPLETED"
    output: dict[str, Any] = Field(default_factory=dict)


class SagaCompletedEvent(BaseSagaIngestEvent):
    event_type: Literal["SAGA_COMPLETED"] = "SAGA_COMPLETED"
    output: dict[str, Any] | None = None


class SagaCompensatedIngestEvent(BaseSagaIngestEvent):
    event_type: Literal["SAGA_COMPENSATED"] = "SAGA_COMPENSATED"
    output: dict[str, Any] | None = None


class SagaFailedIngestEvent(BaseSagaIngestEvent):
    event_type: Literal["SAGA_FAILED"] = "SAGA_FAILED"
    output: dict[str, Any] | None = None


class CompensationFailedIngestEvent(StepLevelSagaIngestEvent):
    event_type: Literal["COMPENSATION_FAILED"] = "COMPENSATION_FAILED"
    error_details: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


class HumanApprovedIngestEvent(StepLevelSagaIngestEvent):
    event_type: Literal["HUMAN_APPROVED"] = "HUMAN_APPROVED"
    output: dict[str, Any] | None = None


class HumanRejectedIngestEvent(StepLevelSagaIngestEvent):
    event_type: Literal["HUMAN_REJECTED"] = "HUMAN_REJECTED"
    error_details: dict[str, Any] | None = None


class HumanRetryIngestEvent(StepLevelSagaIngestEvent):
    event_type: Literal["HUMAN_RETRY"] = "HUMAN_RETRY"
    retry_guidance: str | None = Field(
        default=None,
        max_length=4096,
        description="Optional operator note for this retry; merged into worker _hitl_retry.guidance.",
    )


SagaIngestEvent = Annotated[
    SagaStartedEvent
    | StepFailedEvent
    | StepCompletedIngestEvent
    | StepCompensatedIngestEvent
    | CompensationFailedIngestEvent
    | SagaCompletedEvent
    | SagaCompensatedIngestEvent
    | SagaFailedIngestEvent
    | HumanApprovedIngestEvent
    | HumanRejectedIngestEvent
    | HumanRetryIngestEvent,
    Field(discriminator="event_type"),
]


class HumanApprovedOutboxPayload(BaseSagaMessage):
    idempotency_key: str = Field(..., min_length=8, max_length=256)
    output: dict[str, Any] | None = Field(
        default=None,
        description="Optional output override for reason-step approve-with-edit.",
    )


class HumanRejectedOutboxPayload(BaseSagaMessage):
    idempotency_key: str = Field(..., min_length=8, max_length=256)
    error_details: dict[str, Any] | None = None


class HumanRetryOutboxPayload(BaseSagaMessage):
    idempotency_key: str = Field(
        ...,
        min_length=8,
        max_length=256,
        description="Idempotency for this retry request (duplicate POST with same key returns already_queued).",
    )
    retry_guidance: str | None = Field(
        default=None,
        max_length=4096,
        description="Optional operator note for this retry run.",
    )
