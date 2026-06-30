"""Pydantic schemas for engine API request/response bodies."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response for GET /v1/health."""

    status: str = Field(default="ok", description="Liveness: API process is up")


class StartSagaRequest(BaseModel):
    """Request body for POST /v1/sagas/start."""

    namespace: str = Field(default="default", description="Saga definition namespace")
    name: str = Field(..., description="Saga definition name")
    version: str = Field(..., description="Saga definition version")
    input: dict[str, Any] = Field(default_factory=dict, description="Initial saga context input")
    idempotency_key: str | None = Field(
        default=None,
        description="Client idempotency key; when set, returns existing saga trace_id if one was already started with this key in this namespace.",
    )


class StartSagaResponse(BaseModel):
    """Response body for POST /v1/sagas/start (202 Accepted)."""

    trace_id: str = Field(..., description="Created saga instance trace_id (32-char hex)")


class ManifestDeployResponse(BaseModel):
    """Response body for POST /v1/manifests (200 OK)."""

    message: str = Field(..., description="Success message from registry")


class HumanApproveRequest(BaseModel):
    """Optional body for human approval."""

    output: dict[str, Any] | None = Field(
        default=None,
        description="Optional output override for reason-step approve-with-edit.",
    )


class HumanRejectRequest(BaseModel):
    """Optional body for human rejection."""

    error_details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured rejection reason.",
    )


class HumanRetryRequest(BaseModel):
    """Optional body for manual HITL retry."""

    retry_token: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description=(
            "Optional idempotency token for this HTTP request; "
            "omit to generate a new token on each call."
        ),
    )
    guidance: str | None = Field(
        default=None,
        max_length=4096,
        description=(
            "Operator note for this retry run, passed to the worker as _hitl_retry.guidance "
            "(overrides the step manifest hitl_retry_guidance for this attempt)."
        ),
    )


class OperatorRecoveryRequest(BaseModel):
    """Body for operator retry-step / retry-compensation."""

    recovery_token: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description=(
            "Optional client-supplied token (8–128 chars). When provided, duplicate recovery "
            "requests with the same token and parameters return the original 202 response "
            "without re-enqueueing work."
        ),
    )
    force: bool = Field(
        default=False,
        description="Release a non-stale worker claim (reason steps; commit requires allow_destructive).",
    )
    allow_destructive: bool = Field(
        default=False,
        description="Required with force on commit steps (duplicate side-effect risk).",
    )
    reason: str | None = Field(
        default=None,
        max_length=4096,
        description="Optional operator note for audit hooks.",
    )


class HumanDecisionRequest(BaseModel):
    """Body for a HITL decision."""

    decision: Literal["APPROVE", "REJECT"] = Field(
        ...,
        description="Human decision for this step.",
    )
    output: dict[str, Any] | None = Field(
        default=None,
        description="Optional output override when approving a reason step.",
    )
    error_details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured rejection reason.",
    )


class SagaDefinitionItem(BaseModel):
    """One row in GET /v1/definitions/sagas."""

    id: str = Field(..., description="Saga definition UUID")
    namespace: str
    name: str
    version: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SagaDefinitionListResponse(BaseModel):
    """Response for GET /v1/definitions/sagas."""

    items: list[SagaDefinitionItem]
    limit: int
    offset: int


class WorkerDefinitionItem(BaseModel):
    """One row in GET /v1/definitions/workers."""

    id: str = Field(..., description="Worker definition UUID")
    namespace: str
    name: str
    version: str
    adapter: str
    created_at: datetime
    updated_at: datetime


class WorkerDefinitionListResponse(BaseModel):
    """Response for GET /v1/definitions/workers."""

    items: list[WorkerDefinitionItem]
    limit: int
    offset: int


class SagaInstanceItem(BaseModel):
    """One row in GET /v1/sagas (instance list)."""

    trace_id: str
    namespace: str
    definition_id: str
    status: str
    started_at: datetime
    start_idempotency_key: str | None = None


class SagaInstanceListResponse(BaseModel):
    """Response for GET /v1/sagas."""

    items: list[SagaInstanceItem]
    limit: int
    offset: int


class SagaStepInstanceItem(BaseModel):
    """One row in GET /v1/sagas/steps."""

    step_span_id: str
    saga_trace_id: str
    namespace: str
    step_id: str
    step_name: str
    step_kind: str
    order_index: int
    status: str
    worker: str
    worker_version: str
    started_at: datetime
    end_time: datetime | None = None
    compensates_span_id: str | None = None
    error_details: dict[str, Any] | None = Field(
        default=None,
        description="Structured failure metadata (code, message, tool) when the step errors.",
    )
    timing: dict[str, Any] | None = Field(
        default=None,
        description="Merged worker/engine execution timing buckets (milliseconds).",
    )


class SagaStepInstanceListResponse(BaseModel):
    """Response for GET /v1/sagas/steps."""

    items: list[SagaStepInstanceItem]
    limit: int
    offset: int


class SagaStepInstanceDetail(SagaStepInstanceItem):
    """One saga step instance with execution payloads (GET .../steps/{step_span_id})."""

    resolved_arguments: dict[str, Any] | None = Field(
        default=None,
        description="Evaluated step inputs after JSONPath resolution.",
    )
    output_payload: dict[str, Any] | None = Field(
        default=None,
        description="Worker output on complete/fail; business data often under output.data.",
    )
    prompt_ref: str | None = Field(
        default=None,
        description="Prompt template filename for reason steps.",
    )


class PendingReviewStepItem(BaseModel):
    """One HITL-held step awaiting human approval/rejection."""

    namespace: str
    saga_trace_id: str
    step_span_id: str
    step_id: str
    step_name: str
    step_kind: Literal["reason", "commit"]
    order_index: int
    worker: str
    worker_version: str
    review_subject: str
    review_payload: dict[str, Any]
    started_at: datetime


class PendingReviewStepListResponse(BaseModel):
    """Response for GET /v1/sagas/pending-review."""

    items: list[PendingReviewStepItem]
    limit: int
    offset: int
