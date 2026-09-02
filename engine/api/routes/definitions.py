"""Definition list API: GET /v1/definitions/sagas, GET /v1/definitions/workers."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from engine.api import read_queries
from engine.api.ids import validate_definition_id, validate_namespace
from engine.api.pagination import validated_limit_offset
from engine.api.schemas import (
    SagaDefinitionItem,
    SagaDefinitionListResponse,
    WorkerDefinitionItem,
    WorkerDefinitionListResponse,
)
from engine.registry.service import worker_manifest_body_from_row

router = APIRouter(prefix="/definitions", tags=["definitions"])


def _has_more(items: list[Any], limit: int) -> bool:
    return len(items) == limit


def _saga_definition_item(row: Any, *, include_body: bool) -> SagaDefinitionItem:
    return SagaDefinitionItem(
        id=str(row.id),
        namespace=row.namespace,
        name=row.name,
        version=row.version,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        body=row.body if include_body else None,
    )


def _worker_definition_item(row: Any, *, include_body: bool) -> WorkerDefinitionItem:
    return WorkerDefinitionItem(
        id=str(row.id),
        namespace=row.namespace,
        name=row.name,
        version=row.version,
        adapter=row.adapter,
        created_at=row.created_at,
        updated_at=row.updated_at,
        body=worker_manifest_body_from_row(row) if include_body else None,
    )


@router.get("/sagas", response_model=SagaDefinitionListResponse)
async def get_definitions_sagas(
    namespace: str | None = Query(default=None, description="Filter by namespace; omit for all."),
    name: str | None = Query(default=None, description="Exact saga definition name."),
    is_active: bool | None = Query(default=None, description="Filter by active flag."),
    include_total: bool = Query(default=False, description="Include total matching row count."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> SagaDefinitionListResponse:
    """List registered saga definitions, newest updates first."""
    if namespace is not None:
        validate_namespace(namespace)
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    rows = await read_queries.list_saga_definitions(
        namespace=namespace,
        name=name,
        is_active=is_active,
        limit=lim,
        offset=off,
    )
    items = [_saga_definition_item(r, include_body=False) for r in rows]
    total = None
    if include_total:
        total = await read_queries.count_saga_definitions(
            namespace=namespace,
            name=name,
            is_active=is_active,
        )
    return SagaDefinitionListResponse(
        items=items,
        limit=lim,
        offset=off,
        has_more=_has_more(rows, lim),
        total=total,
    )


@router.get("/sagas/{definition_id}", response_model=SagaDefinitionItem)
async def get_definitions_saga_by_id(
    definition_id: str,
    include_body: bool = Query(
        default=False,
        description="When true, include the full manifest blueprint in body.",
    ),
) -> SagaDefinitionItem:
    """Return one saga definition by primary key UUID (for start-saga resolution)."""
    uid = validate_definition_id(definition_id)
    row = await read_queries.get_saga_definition_by_uuid(definition_id=uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Saga definition not found for id={definition_id!r}.",
        )
    return _saga_definition_item(row, include_body=include_body)


@router.get("/workers", response_model=WorkerDefinitionListResponse)
async def get_definitions_workers(
    namespace: str | None = Query(default=None, description="Filter by namespace; omit for all."),
    name: str | None = Query(default=None, description="Exact worker definition name."),
    include_total: bool = Query(default=False, description="Include total matching row count."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> WorkerDefinitionListResponse:
    """List registered worker definitions, newest updates first."""
    if namespace is not None:
        validate_namespace(namespace)
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    rows = await read_queries.list_worker_definitions(
        namespace=namespace,
        name=name,
        limit=lim,
        offset=off,
    )
    items = [_worker_definition_item(r, include_body=False) for r in rows]
    total = None
    if include_total:
        total = await read_queries.count_worker_definitions(namespace=namespace, name=name)
    return WorkerDefinitionListResponse(
        items=items,
        limit=lim,
        offset=off,
        has_more=_has_more(rows, lim),
        total=total,
    )


@router.get("/workers/{definition_id}", response_model=WorkerDefinitionItem)
async def get_definitions_worker_by_id(
    definition_id: str,
    include_body: bool = Query(
        default=False,
        description="When true, include the full manifest body.",
    ),
) -> WorkerDefinitionItem:
    """Return one worker definition by primary key UUID."""
    uid = validate_definition_id(definition_id)
    row = await read_queries.get_worker_definition_by_uuid(definition_id=uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Worker definition not found for id={definition_id!r}.",
        )
    return _worker_definition_item(row, include_body=include_body)
