"""Definition list/get/patch API: GET|PATCH /v1/definitions/{sagas,workers,steps}."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from common.definition_identity import DefinitionIdentityError, resolve_definition_identity
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from engine.api import read_queries
from engine.api.ids import validate_definition_id, validate_namespace
from engine.api.pagination import validated_limit_offset
from engine.api.schemas import (
    DefinitionActiveUpdate,
    SagaDefinitionItem,
    SagaDefinitionListResponse,
    StepDefinitionItem,
    StepDefinitionListResponse,
    WorkerDefinitionItem,
    WorkerDefinitionListResponse,
)

router = APIRouter(prefix="/definitions", tags=["definitions"])

ItemT = TypeVar("ItemT", bound=BaseModel)
ListT = TypeVar("ListT", bound=BaseModel)

ListFn = Callable[..., Awaitable[list[Any]]]
CountFn = Callable[..., Awaitable[int]]
GetByUuidFn = Callable[..., Awaitable[Any | None]]
GetByTripleFn = Callable[..., Awaitable[Any | None]]


def _has_more(items: list[Any], limit: int) -> bool:
    return len(items) == limit


def _definition_item(
    row: Any,
    *,
    item_cls: type[ItemT],
    include_body: bool,
) -> ItemT:
    return item_cls(
        id=str(row.id),
        namespace=row.namespace,
        name=row.name,
        version=row.version,
        is_active=bool(row.is_active),
        created_at=row.created_at,
        updated_at=row.updated_at,
        body=row.body if include_body else None,
    )


async def _set_definition_active(*, row: Any, is_active: bool) -> Any:
    if bool(row.is_active) == is_active:
        return row
    row.is_active = is_active
    row.updated_at = datetime.now(UTC)
    await row.save(update_fields=["is_active", "updated_at"])
    return row


def _resolve_patch_identity(
    *,
    kind_label: str,
    definition_id: str | None,
    namespace: str | None,
    name: str | None,
    version: str | None,
) -> tuple[str | None, tuple[str, str, str] | None]:
    """Return (uuid, triple) with exactly one identity set."""
    try:
        identity = resolve_definition_identity(
            definition_id=definition_id,
            namespace=namespace,
            name=name,
            version=version,
            label=f"{kind_label} PATCH",
        )
    except DefinitionIdentityError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return identity.definition_id, identity.triple


async def _list_definitions(
    *,
    item_cls: type[ItemT],
    list_response_cls: type[ListT],
    list_fn: ListFn,
    count_fn: CountFn,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
    include_total: bool,
    limit: int | None,
    offset: int | None,
) -> ListT:
    if namespace is not None:
        validate_namespace(namespace)
    lim, off = validated_limit_offset(limit=limit, offset=offset)
    rows = await list_fn(
        namespace=namespace,
        name=name,
        is_active=is_active,
        limit=lim,
        offset=off,
    )
    items = [_definition_item(r, item_cls=item_cls, include_body=False) for r in rows]
    total = None
    if include_total:
        total = await count_fn(namespace=namespace, name=name, is_active=is_active)
    return list_response_cls(
        items=items,
        limit=lim,
        offset=off,
        has_more=_has_more(rows, lim),
        total=total,
    )


async def _get_definition_by_id(
    *,
    kind_label: str,
    item_cls: type[ItemT],
    get_by_uuid: GetByUuidFn,
    definition_id: str,
    include_body: bool,
) -> ItemT:
    uid = validate_definition_id(definition_id)
    row = await get_by_uuid(definition_id=uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"{kind_label} definition not found for id={definition_id!r}.",
        )
    return _definition_item(row, item_cls=item_cls, include_body=include_body)


async def _patch_definition_active(
    *,
    kind_label: str,
    item_cls: type[ItemT],
    get_by_uuid: GetByUuidFn,
    get_by_triple: GetByTripleFn,
    payload: DefinitionActiveUpdate,
    definition_id: str | None,
    namespace: str | None,
    name: str | None,
    version: str | None,
) -> ItemT:
    uuid_raw, triple = _resolve_patch_identity(
        kind_label=kind_label,
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
    )
    if uuid_raw is not None:
        uid = validate_definition_id(uuid_raw)
        row = await get_by_uuid(definition_id=uid)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"{kind_label} definition not found for id={uuid_raw!r}.",
            )
    elif triple is not None:
        ns, n, ver = triple
        validate_namespace(ns)
        row = await get_by_triple(namespace=ns, name=n, version=ver)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{kind_label} definition not found: "
                    f"namespace={ns!r}, name={n!r}, version={ver!r}."
                ),
            )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"{kind_label} PATCH requires id= or namespace+name+version query parameters.",
        )
    row = await _set_definition_active(row=row, is_active=payload.is_active)
    return _definition_item(row, item_cls=item_cls, include_body=False)


# --- sagas -----------------------------------------------------------------


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
    return await _list_definitions(
        item_cls=SagaDefinitionItem,
        list_response_cls=SagaDefinitionListResponse,
        list_fn=read_queries.list_saga_definitions,
        count_fn=read_queries.count_saga_definitions,
        namespace=namespace,
        name=name,
        is_active=is_active,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


@router.patch("/sagas", response_model=SagaDefinitionItem)
async def patch_definitions_saga_active(
    payload: DefinitionActiveUpdate,
    definition_id: str | None = Query(
        default=None, alias="id", description="Definition UUID (mutually exclusive with triple)."
    ),
    namespace: str | None = Query(default=None, description="With name+version: identity triple."),
    name: str | None = Query(default=None, description="With namespace+version: identity triple."),
    version: str | None = Query(default=None, description="With namespace+name: identity triple."),
) -> SagaDefinitionItem:
    """Soft-enable or soft-disable a saga definition (`is_active`) by id or triple."""
    return await _patch_definition_active(
        kind_label="Saga",
        item_cls=SagaDefinitionItem,
        get_by_uuid=read_queries.get_saga_definition_by_uuid,
        get_by_triple=read_queries.get_saga_definition_by_triple,
        payload=payload,
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
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
    return await _get_definition_by_id(
        kind_label="Saga",
        item_cls=SagaDefinitionItem,
        get_by_uuid=read_queries.get_saga_definition_by_uuid,
        definition_id=definition_id,
        include_body=include_body,
    )


# --- workers ---------------------------------------------------------------


@router.get("/workers", response_model=WorkerDefinitionListResponse)
async def get_definitions_workers(
    namespace: str | None = Query(default=None, description="Filter by namespace; omit for all."),
    name: str | None = Query(default=None, description="Exact worker definition name."),
    is_active: bool | None = Query(default=None, description="Filter by active flag."),
    include_total: bool = Query(default=False, description="Include total matching row count."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> WorkerDefinitionListResponse:
    """List registered worker definitions, newest updates first."""
    return await _list_definitions(
        item_cls=WorkerDefinitionItem,
        list_response_cls=WorkerDefinitionListResponse,
        list_fn=read_queries.list_worker_definitions,
        count_fn=read_queries.count_worker_definitions,
        namespace=namespace,
        name=name,
        is_active=is_active,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


@router.patch("/workers", response_model=WorkerDefinitionItem)
async def patch_definitions_worker_active(
    payload: DefinitionActiveUpdate,
    definition_id: str | None = Query(
        default=None, alias="id", description="Definition UUID (mutually exclusive with triple)."
    ),
    namespace: str | None = Query(default=None, description="With name+version: identity triple."),
    name: str | None = Query(default=None, description="With namespace+version: identity triple."),
    version: str | None = Query(default=None, description="With namespace+name: identity triple."),
) -> WorkerDefinitionItem:
    """Soft-enable or soft-disable a worker definition (`is_active`) by id or triple."""
    return await _patch_definition_active(
        kind_label="Worker",
        item_cls=WorkerDefinitionItem,
        get_by_uuid=read_queries.get_worker_definition_by_uuid,
        get_by_triple=read_queries.get_worker_definition_by_triple,
        payload=payload,
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
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
    return await _get_definition_by_id(
        kind_label="Worker",
        item_cls=WorkerDefinitionItem,
        get_by_uuid=read_queries.get_worker_definition_by_uuid,
        definition_id=definition_id,
        include_body=include_body,
    )


# --- steps -----------------------------------------------------------------


@router.get("/steps", response_model=StepDefinitionListResponse)
async def get_definitions_steps(
    namespace: str | None = Query(default=None, description="Filter by namespace; omit for all."),
    name: str | None = Query(default=None, description="Exact step definition name."),
    is_active: bool | None = Query(default=None, description="Filter by active flag."),
    include_total: bool = Query(default=False, description="Include total matching row count."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> StepDefinitionListResponse:
    """List registered step definitions, newest updates first."""
    return await _list_definitions(
        item_cls=StepDefinitionItem,
        list_response_cls=StepDefinitionListResponse,
        list_fn=read_queries.list_step_definitions,
        count_fn=read_queries.count_step_definitions,
        namespace=namespace,
        name=name,
        is_active=is_active,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


@router.patch("/steps", response_model=StepDefinitionItem)
async def patch_definitions_step_active(
    payload: DefinitionActiveUpdate,
    definition_id: str | None = Query(
        default=None, alias="id", description="Definition UUID (mutually exclusive with triple)."
    ),
    namespace: str | None = Query(default=None, description="With name+version: identity triple."),
    name: str | None = Query(default=None, description="With namespace+version: identity triple."),
    version: str | None = Query(default=None, description="With namespace+name: identity triple."),
) -> StepDefinitionItem:
    """Soft-enable or soft-disable a step definition (`is_active`) by id or triple."""
    return await _patch_definition_active(
        kind_label="Step",
        item_cls=StepDefinitionItem,
        get_by_uuid=read_queries.get_step_definition_by_uuid,
        get_by_triple=read_queries.get_step_definition_by_triple,
        payload=payload,
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
    )


@router.get("/steps/{definition_id}", response_model=StepDefinitionItem)
async def get_definitions_step_by_id(
    definition_id: str,
    include_body: bool = Query(
        default=False,
        description="When true, include the full step manifest blueprint in body.",
    ),
) -> StepDefinitionItem:
    """Return one step definition by primary key UUID."""
    return await _get_definition_by_id(
        kind_label="Step",
        item_cls=StepDefinitionItem,
        get_by_uuid=read_queries.get_step_definition_by_uuid,
        definition_id=definition_id,
        include_body=include_body,
    )
