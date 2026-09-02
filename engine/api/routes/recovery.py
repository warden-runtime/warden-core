"""Operator saga recovery API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from engine.api.ids import validate_saga_step_path_params
from engine.api.schemas import OperatorRecoveryRequest, RecoveryResponse
from engine.recovery import enqueue_compensation_retry, enqueue_step_retry
from engine.recovery_errors import RecoveryConflictError, RecoveryNotFoundError

router = APIRouter(prefix="/sagas", tags=["recovery"])

_MUTATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Step or saga not found."},
    409: {"description": "Recovery conflict (wrong status, stale claim, etc.)."},
}


def _http_error_from_recovery(exc: Exception) -> HTTPException:
    if isinstance(exc, RecoveryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RecoveryConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    raise exc


@router.post(
    "/{trace_id}/steps/{step_span_id}/retry-step",
    status_code=202,
    response_model=RecoveryResponse,
    responses=_MUTATION_RESPONSES,
)
async def operator_retry_step(
    trace_id: str,
    step_span_id: str,
    namespace: str = Query(default="default"),
    body: OperatorRecoveryRequest | None = None,
) -> RecoveryResponse:
    """Retry a stuck forward step (IN_PROGRESS) after automatic recovery is exhausted."""
    validate_saga_step_path_params(
        trace_id=trace_id, step_span_id=step_span_id, namespace=namespace
    )
    payload = body or OperatorRecoveryRequest()
    try:
        data = await enqueue_step_retry(
            namespace=namespace,
            trace_id=trace_id,
            step_span_id=step_span_id,
            recovery_token=payload.recovery_token,
            force=payload.force,
            allow_destructive=payload.allow_destructive,
            reason=payload.reason,
        )
    except (RecoveryNotFoundError, RecoveryConflictError) as exc:
        raise _http_error_from_recovery(exc) from exc
    return RecoveryResponse.model_validate(data)


@router.post(
    "/{trace_id}/steps/{step_span_id}/retry-compensation",
    status_code=202,
    response_model=RecoveryResponse,
    responses=_MUTATION_RESPONSES,
)
async def operator_retry_compensation(
    trace_id: str,
    step_span_id: str,
    namespace: str = Query(default="default"),
    body: OperatorRecoveryRequest | None = None,
) -> RecoveryResponse:
    """Retry a failed or stuck compensation step."""
    validate_saga_step_path_params(
        trace_id=trace_id, step_span_id=step_span_id, namespace=namespace
    )
    payload = body or OperatorRecoveryRequest()
    try:
        data = await enqueue_compensation_retry(
            namespace=namespace,
            trace_id=trace_id,
            step_span_id=step_span_id,
            recovery_token=payload.recovery_token,
            force=payload.force,
            reason=payload.reason,
        )
    except (RecoveryNotFoundError, RecoveryConflictError) as exc:
        raise _http_error_from_recovery(exc) from exc
    return RecoveryResponse.model_validate(data)
