"""Saga API routes."""

import logging
from typing import Annotated

from common.catalog_errors import CatalogError
from common.models import SagaStatus
from fastapi import APIRouter, HTTPException, Query

from engine.api import read_queries
from engine.api.http_errors import http_exception_for_catalog, structured_detail
from engine.api.ids import validate_namespace, validate_step_span_id, validate_trace_id
from engine.api.pagination import validated_limit_offset
from engine.api.saga_errors import StartIdempotencyConflictError
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


def _saga_instance_item(row) -> SagaInstanceItem:
    return SagaInstanceItem(
        trace_id=row.trace_id,
        namespace=row.namespace,
        definition_id=row.definition_id,
        definition_name=row.definition_name,
        definition_version=row.definition_version,
        status=row.status.value,
        started_at=row.started_at,
        start_idempotency_key=row.start_idempotency_key,
        parent_trace_id=row.parent_trace_id,
    )


def _resolve_list_saga_statuses(
    *,
    in_flight: bool | None,
    failed: bool | None,
    status_list: list[str],
) -> list[SagaStatus] | None:
    filter_flags = sum(flag is True for flag in (in_flight, failed))
    if filter_flags > 1:
        raise HTTPException(
            status_code=400,
            detail="Use only one of in_flight=true, failed=true, or explicit status filters.",
        )
    if filter_flags == 1 and len(status_list) > 0:
        raise HTTPException(
            status_code=400,
            detail="Do not combine in_flight/failed with status filters; use one or the other.",
        )
    try:
        if in_flight is True:
            return read_queries.in_flight_statuses()
        if failed is True:
            return [SagaStatus.FAILED]
        if len(status_list) > 0:
            return read_queries.parse_saga_statuses(status_list)
        return None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


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
    parent_trace_id: Annotated[
        str | None,
        Query(description="Filter to child sagas spawned by this parent trace_id."),
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
    failed: Annotated[
        bool | None,
        Query(
            description="When true, filter to FAILED sagas only (mutually exclusive with in_flight and status).",
        ),
    ] = None,
    include_total: Annotated[
        bool,
        Query(description="Include total matching row count."),
    ] = False,
    limit: Annotated[int | None, Query()] = None,
    offset: Annotated[int | None, Query()] = None,
) -> SagaInstanceListResponse:
    """List saga instances, newest started_at first."""
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    if trace_id is not None:
        validate_trace_id(trace_id)
    if parent_trace_id is not None:
        validate_trace_id(parent_trace_id)
    if namespace is not None:
        validate_namespace(namespace)
    statuses = _resolve_list_saga_statuses(
        in_flight=in_flight,
        failed=failed,
        status_list=list(status) if status is not None else [],
    )

    rows = await read_queries.list_saga_instances(
        namespace=namespace,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        statuses=statuses,
        limit=lim,
        offset=off,
    )
    items = [_saga_instance_item(r) for r in rows]
    total = None
    if include_total:
        total = await read_queries.count_saga_instances(
            namespace=namespace,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            statuses=statuses,
        )
    return SagaInstanceListResponse(
        items=items,
        limit=lim,
        offset=off,
        has_more=len(items) == lim,
        total=total,
    )


async def _list_steps_for_saga(
    *,
    trace_id: str,
    namespace: str | None,
    status: list[str] | None,
    include_total: bool,
    limit: int | None,
    offset: int | None,
) -> SagaStepInstanceListResponse:
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
    total = None
    if include_total:
        total = await read_queries.count_saga_step_instances(
            saga_trace_id=trace_id,
            namespace=namespace,
            statuses=statuses,
        )
    return SagaStepInstanceListResponse(
        items=items,
        limit=lim,
        offset=off,
        has_more=len(items) == lim,
        total=total,
    )


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
    include_total: Annotated[
        bool,
        Query(description="Include total matching row count."),
    ] = False,
    limit: Annotated[int | None, Query()] = None,
    offset: Annotated[int | None, Query()] = None,
) -> SagaStepInstanceListResponse:
    """List step instances for one saga, ordered by forward_seq."""
    return await _list_steps_for_saga(
        trace_id=trace_id,
        namespace=namespace,
        status=status,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


@router.get("/{trace_id}", response_model=SagaInstanceItem)
async def get_saga(
    trace_id: str,
    namespace: Annotated[
        str | None,
        Query(description="Optional namespace guard; must match the saga row."),
    ] = None,
) -> SagaInstanceItem:
    """Return one saga instance by trace_id (404 if missing)."""
    validate_trace_id(trace_id)
    if namespace is not None:
        validate_namespace(namespace)
    saga = await read_queries.get_saga_instance(namespace=namespace, trace_id=trace_id)
    if saga is None:
        raise HTTPException(status_code=404, detail="Saga instance not found.")
    return _saga_instance_item(saga)


@router.get("/{trace_id}/steps", response_model=SagaStepInstanceListResponse)
async def get_saga_steps_by_path(
    trace_id: str,
    namespace: Annotated[
        str | None,
        Query(description="Optional namespace guard; must match the saga row."),
    ] = None,
    status: Annotated[
        list[str] | None,
        Query(description="Repeat for multiple step statuses."),
    ] = None,
    include_total: Annotated[
        bool,
        Query(description="Include total matching row count."),
    ] = False,
    limit: Annotated[int | None, Query()] = None,
    offset: Annotated[int | None, Query()] = None,
) -> SagaStepInstanceListResponse:
    """List step instances for one saga (path form of GET /v1/sagas/steps)."""
    return await _list_steps_for_saga(
        trace_id=trace_id,
        namespace=namespace,
        status=status,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


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
        HTTPException: 404 if definition not found; 409 on idempotency conflict or inactive
            catalog definition (structured ``INACTIVE_CATALOG_DEFINITION`` /
            ``CATALOG_DEFINITION_NOT_FOUND``); 400 on other ValueError.
    """
    try:
        result = await start_saga(
            namespace=body.namespace,
            name=body.name,
            version=body.version,
            input=body.input,
            idempotency_key=body.idempotency_key,
        )
        return StartSagaResponse(trace_id=result.trace_id, created=result.created)
    except CatalogError as e:
        raise http_exception_for_catalog(e) from e
    except StartIdempotencyConflictError as e:
        raise HTTPException(
            status_code=409,
            detail=structured_detail(
                code="START_IDEMPOTENCY_CONFLICT",
                message=str(e),
                namespace=e.namespace,
                idempotency_key=e.idempotency_key,
                existing_definition_id=e.existing_definition_id,
            ),
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
