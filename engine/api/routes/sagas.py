"""Saga API routes."""

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from engine.api import read_queries
from engine.api.ids import validate_namespace, validate_step_span_id, validate_trace_id
from engine.api.pagination import validated_limit_offset
from engine.api.saga_start import start_saga
from engine.api.schemas import (
    SagaInstanceItem,
    SagaInstanceListResponse,
    SagaStepInstanceDetail,
    SagaStepInstanceListResponse,
    StartSagaRequest,
    StartSagaResponse,
)
from engine.api.step_serializers import (
    saga_step_instance_detail_from_row,
    saga_step_instance_item_from_row,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sagas", tags=["sagas"])


@router.get("", response_model=SagaInstanceListResponse)
async def get_sagas(
    namespace: Annotated[
        str | None,
        Query(description="Filter by namespace; omit for all."),
    ] = None,
    trace_id: Annotated[
        str | None,
        Query(description="Filter to a single saga instance trace_id."),
    ] = None,
    status: Annotated[
        list[str] | None,
        Query(
            description="Repeat for multiple values, e.g. status=RUNNING&status=PENDING.",
        ),
    ] = None,
    in_flight: Annotated[
        bool | None,
        Query(
            description="When true, filter to non-terminal in-flight statuses (mutually exclusive with status).",
        ),
    ] = None,
    limit: Annotated[int | None, Query()] = None,
    offset: Annotated[int | None, Query()] = None,
) -> SagaInstanceListResponse:
    """List saga instances, newest started_at first."""
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    if trace_id is not None:
        validate_trace_id(trace_id)
    if namespace is not None:
        validate_namespace(namespace)
    status_list = list(status) if status is not None else []
    if in_flight is True and len(status_list) > 0:
        raise HTTPException(
            status_code=400,
            detail="Do not combine in_flight=true with status filters; use one or the other.",
        )
    try:
        if in_flight is True:
            statuses = read_queries.in_flight_statuses()
        elif len(status_list) > 0:
            statuses = read_queries.parse_saga_statuses(status_list)
        else:
            statuses = None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    rows = await read_queries.list_saga_instances(
        namespace=namespace,
        trace_id=trace_id,
        statuses=statuses,
        limit=lim,
        offset=off,
    )
    items = [
        SagaInstanceItem(
            trace_id=r.trace_id,
            namespace=r.namespace,
            definition_id=r.definition_id,
            status=r.status.value,
            started_at=r.started_at,
            start_idempotency_key=r.start_idempotency_key,
        )
        for r in rows
    ]
    return SagaInstanceListResponse(items=items, limit=lim, offset=off)


@router.get("/steps", response_model=SagaStepInstanceListResponse)
async def get_saga_steps(
    trace_id: Annotated[
        str,
        Query(description="Saga instance trace_id (32-char hex)."),
    ],
    namespace: Annotated[
        str | None,
        Query(description="Optional namespace guard; must match the saga row."),
    ] = None,
    status: Annotated[
        list[str] | None,
        Query(description="Repeat for multiple step statuses."),
    ] = None,
    limit: Annotated[int | None, Query()] = None,
    offset: Annotated[int | None, Query()] = None,
) -> SagaStepInstanceListResponse:
    """List step instances for one saga, ordered by order_index."""
    validate_trace_id(trace_id)
    if namespace is not None:
        validate_namespace(namespace)
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    status_list = list(status) if status is not None else []
    try:
        statuses = read_queries.parse_step_statuses(status_list) if status_list else None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    saga = await read_queries.get_saga_instance(namespace=namespace, trace_id=trace_id)
    if saga is None:
        raise HTTPException(status_code=404, detail="Saga instance not found.")

    rows = await read_queries.list_saga_step_instances(
        saga_trace_id=trace_id,
        namespace=namespace,
        statuses=statuses,
        limit=lim,
        offset=off,
    )
    items = [saga_step_instance_item_from_row(r) for r in rows]
    return SagaStepInstanceListResponse(items=items, limit=lim, offset=off)


@router.get("/{trace_id}/steps/{step_span_id}", response_model=SagaStepInstanceDetail)
async def get_saga_step_detail(
    trace_id: str,
    step_span_id: str,
    namespace: Annotated[
        str | None,
        Query(description="Optional namespace guard; must match the saga row."),
    ] = None,
) -> SagaStepInstanceDetail:
    """Return one step instance with resolved inputs and output payloads."""
    validate_trace_id(trace_id)
    validate_step_span_id(step_span_id)
    if namespace is not None:
        validate_namespace(namespace)

    saga = await read_queries.get_saga_instance(namespace=namespace, trace_id=trace_id)
    if saga is None:
        raise HTTPException(status_code=404, detail="Saga instance not found.")

    row = await read_queries.get_saga_step_instance(
        saga_trace_id=trace_id,
        step_span_id=step_span_id,
        namespace=namespace,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Saga step instance not found.")
    return saga_step_instance_detail_from_row(row)


@router.post("/start", response_model=StartSagaResponse, status_code=202)
async def post_sagas_start(body: StartSagaRequest) -> StartSagaResponse:
    """Start a saga from a registered definition; returns 202 with trace_id.

    Creates saga and step instances in a transaction and emits SAGA_STARTED.

    Args:
        body: Namespace, name, version, and input for the saga.

    Returns:
        StartSagaResponse with trace_id.

    Raises:
        HTTPException: 404 if definition not found; 400 on other ValueError.
    """
    try:
        trace_id = await start_saga(
            namespace=body.namespace,
            name=body.name,
            version=body.version,
            input=body.input,
            idempotency_key=body.idempotency_key,
        )
        return StartSagaResponse(trace_id=trace_id)
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e)) from e
        raise HTTPException(status_code=400, detail=str(e)) from e
