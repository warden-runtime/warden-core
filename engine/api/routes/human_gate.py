"""HITL approval/rejection API routes."""

from typing import Any

from common.hitl_retry import HitlRetryLimitError
from common.schemas.saga import WORKER_STEP_KINDS
from fastapi import APIRouter, HTTPException, Query

from engine.api import read_queries
from engine.api.ids import (
    validate_namespace,
    validate_saga_step_path_params,
    validation_http_exception,
)
from engine.api.pagination import validated_limit_offset
from engine.api.schemas import (
    HumanApproveRequest,
    HumanDecisionRequest,
    HumanDecisionResponse,
    HumanRejectRequest,
    HumanRetryRequest,
    HumanRetryResponse,
    PendingReviewStepItem,
    PendingReviewStepListResponse,
)
from engine.hitl_decisions import (
    HumanDecisionConflictError,
    HumanDecisionNotFoundError,
    InvalidHumanDecisionError,
    enqueue_hitl_retry,
    enqueue_human_decision,
)

router = APIRouter(prefix="/sagas", tags=["hitl"])

_MUTATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Step not found."},
    409: {"description": "Conflict (e.g. step not awaiting human review)."},
    422: {"description": "Invalid request."},
}


def _http_error_from_decision(exc: Exception) -> HTTPException:
    if isinstance(exc, HumanDecisionNotFoundError):
        return HTTPException(status_code=404, detail="Step not found.")
    if isinstance(exc, HumanDecisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidHumanDecisionError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.get("/pending-review", response_model=PendingReviewStepListResponse)
async def pending_review_steps(
    namespace: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    kind: str | None = Query(default=None, description="Optional step kind: reason or commit."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> PendingReviewStepListResponse:
    """List HITL-held steps awaiting human approval/rejection."""
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    if namespace is not None:
        validate_namespace(namespace)
    step_kind = kind.strip().lower() if kind else None
    if step_kind is not None and step_kind not in WORKER_STEP_KINDS:
        raise validation_http_exception(
            f"kind must be one of {', '.join(sorted(WORKER_STEP_KINDS))}.",
            loc=["query", "kind"],
        )

    rows = await read_queries.list_pending_review_steps(
        namespace=namespace,
        saga_trace_id=trace_id,
        step_kind=step_kind,
        limit=lim,
        offset=off,
    )
    items: list[PendingReviewStepItem] = []
    for row in rows:
        if row.step_kind == "commit":
            subject = "arguments"
            payload = row.resolved_arguments or {}
        else:
            subject = "output"
            payload = row.pending_review_payload or {}
        items.append(
            PendingReviewStepItem(
                namespace=row.namespace,
                saga_trace_id=row.saga_trace_id,
                step_span_id=row.span_id,
                step_id=row.step_id,
                step_name=row.step_name,
                step_kind=row.step_kind,
                order_index=row.order_index,
                worker=row.worker,
                worker_version=row.worker_version,
                review_subject=subject,
                review_payload=payload,
                started_at=row.started_at,
            )
        )
    return PendingReviewStepListResponse(
        items=items,
        limit=lim,
        offset=off,
        has_more=len(items) == lim,
    )


async def _enqueue_human_decision(**kwargs) -> dict[str, str]:
    try:
        return await enqueue_human_decision(**kwargs)
    except (HumanDecisionNotFoundError, HumanDecisionConflictError, InvalidHumanDecisionError) as e:
        raise _http_error_from_decision(e) from e


async def _enqueue_hitl_retry(**kwargs) -> dict[str, str]:
    try:
        return await enqueue_hitl_retry(**kwargs)
    except HitlRetryLimitError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (HumanDecisionNotFoundError, HumanDecisionConflictError) as e:
        raise _http_error_from_decision(e) from e


@router.post(
    "/{trace_id}/steps/{step_span_id}/decision",
    status_code=202,
    response_model=HumanDecisionResponse,
    responses=_MUTATION_RESPONSES,
)
async def decide_step(
    trace_id: str,
    step_span_id: str,
    body: HumanDecisionRequest,
    namespace: str = Query(default="default"),
) -> HumanDecisionResponse:
    """Submit one idempotent human decision for a HITL-held step."""
    validate_saga_step_path_params(
        trace_id=trace_id, step_span_id=step_span_id, namespace=namespace
    )
    data = await _enqueue_human_decision(
        trace_id=trace_id,
        step_span_id=step_span_id,
        namespace=namespace,
        decision=body.decision,
        output=body.output,
        error_details=body.error_details,
    )
    return HumanDecisionResponse.model_validate(data)


@router.post(
    "/{trace_id}/steps/{step_span_id}/approve",
    status_code=202,
    response_model=HumanDecisionResponse,
    responses=_MUTATION_RESPONSES,
)
async def approve_step(
    trace_id: str,
    step_span_id: str,
    namespace: str = Query(default="default"),
    body: HumanApproveRequest | None = None,
) -> HumanDecisionResponse:
    """Approve a HITL-held step and enqueue HUMAN_APPROVED for the engine."""
    validate_saga_step_path_params(
        trace_id=trace_id, step_span_id=step_span_id, namespace=namespace
    )
    data = await _enqueue_human_decision(
        trace_id=trace_id,
        step_span_id=step_span_id,
        namespace=namespace,
        decision="APPROVE",
        output=body.output if body else None,
    )
    return HumanDecisionResponse.model_validate(data)


@router.post(
    "/{trace_id}/steps/{step_span_id}/reject",
    status_code=202,
    response_model=HumanDecisionResponse,
    responses=_MUTATION_RESPONSES,
)
async def reject_step(
    trace_id: str,
    step_span_id: str,
    namespace: str = Query(default="default"),
    body: HumanRejectRequest | None = None,
) -> HumanDecisionResponse:
    """Reject a HITL-held step and enqueue HUMAN_REJECTED for the engine."""
    validate_saga_step_path_params(
        trace_id=trace_id, step_span_id=step_span_id, namespace=namespace
    )
    data = await _enqueue_human_decision(
        trace_id=trace_id,
        step_span_id=step_span_id,
        namespace=namespace,
        decision="REJECT",
        error_details=body.error_details if body else None,
    )
    return HumanDecisionResponse.model_validate(data)


@router.post(
    "/{trace_id}/steps/{step_span_id}/retry",
    status_code=202,
    response_model=HumanRetryResponse,
    responses=_MUTATION_RESPONSES,
)
async def retry_step(
    trace_id: str,
    step_span_id: str,
    namespace: str = Query(default="default"),
    body: HumanRetryRequest | None = None,
) -> HumanRetryResponse:
    """Re-run a HITL-held step; preserves upstream saga context (state-preserved retry)."""
    validate_saga_step_path_params(
        trace_id=trace_id, step_span_id=step_span_id, namespace=namespace
    )
    data = await _enqueue_hitl_retry(
        trace_id=trace_id,
        step_span_id=step_span_id,
        namespace=namespace,
        retry_token=body.retry_token if body else None,
        retry_guidance=body.guidance if body else None,
    )
    return HumanRetryResponse.model_validate(data)
