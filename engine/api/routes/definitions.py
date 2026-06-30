"""Definition list API: GET /v1/definitions/sagas, GET /v1/definitions/workers."""

from fastapi import APIRouter, HTTPException, Query

from engine.api import read_queries
from engine.api.pagination import validated_limit_offset
from engine.api.schemas import (
    SagaDefinitionItem,
    SagaDefinitionListResponse,
    WorkerDefinitionItem,
    WorkerDefinitionListResponse,
)

router = APIRouter(prefix="/definitions", tags=["definitions"])


@router.get("/sagas", response_model=SagaDefinitionListResponse)
async def get_definitions_sagas(
    namespace: str | None = Query(default=None, description="Filter by namespace; omit for all."),
    name: str | None = Query(default=None, description="Exact saga definition name."),
    is_active: bool | None = Query(default=None, description="Filter by active flag."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> SagaDefinitionListResponse:
    """List registered saga definitions, newest updates first."""
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    rows = await read_queries.list_saga_definitions(
        namespace=namespace,
        name=name,
        is_active=is_active,
        limit=lim,
        offset=off,
    )
    items = [
        SagaDefinitionItem(
            id=str(r.id),
            namespace=r.namespace,
            name=r.name,
            version=r.version,
            is_active=r.is_active,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
    return SagaDefinitionListResponse(items=items, limit=lim, offset=off)


@router.get("/sagas/{definition_id}", response_model=SagaDefinitionItem)
async def get_definitions_saga_by_id(definition_id: str) -> SagaDefinitionItem:
    """Return one saga definition by primary key UUID (for start-saga resolution)."""
    row = await read_queries.get_saga_definition_by_id(definition_id=definition_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Saga definition not found for id={definition_id!r}.",
        )
    return SagaDefinitionItem(
        id=str(row.id),
        namespace=row.namespace,
        name=row.name,
        version=row.version,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/workers", response_model=WorkerDefinitionListResponse)
async def get_definitions_workers(
    namespace: str | None = Query(default=None, description="Filter by namespace; omit for all."),
    name: str | None = Query(default=None, description="Exact worker definition name."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> WorkerDefinitionListResponse:
    """List registered worker definitions, newest updates first."""
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    rows = await read_queries.list_worker_definitions(
        namespace=namespace,
        name=name,
        limit=lim,
        offset=off,
    )
    items = [
        WorkerDefinitionItem(
            id=str(r.id),
            namespace=r.namespace,
            name=r.name,
            version=r.version,
            adapter=r.adapter,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
    return WorkerDefinitionListResponse(items=items, limit=lim, offset=off)
