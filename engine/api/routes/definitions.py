"""Definition list/get/patch API: GET|PATCH /v1/definitions/{sagas,workers,steps}."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

from common.definition_identity import DefinitionIdentityError, resolve_definition_identity
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from engine.api import read_queries
from engine.api.http_errors import definition_not_found_http
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
CatalogKind = Literal["saga", "step", "worker"]

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


def _resolve_identity(
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
            label=kind_label,
        )
    except DefinitionIdentityError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return identity.definition_id, identity.triple


def _wants_identity_get(*, definition_id: str | None, version: str | None) -> bool:
    """True when collection GET should return one item (id= or version= present)."""
    return definition_id is not None or version is not None


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


async def _get_definition(
    *,
    kind: CatalogKind,
    kind_label: str,
    item_cls: type[ItemT],
    get_by_uuid: GetByUuidFn,
    get_by_triple: GetByTripleFn,
    definition_id: str | None,
    namespace: str | None,
    name: str | None,
    version: str | None,
    include_body: bool,
) -> ItemT:
    uuid_raw, triple = _resolve_identity(
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
            raise definition_not_found_http(kind=kind, definition_id=uuid_raw)
    elif triple is not None:
        ns, n, ver = triple
        validate_namespace(ns)
        row = await get_by_triple(namespace=ns, name=n, version=ver)
        if row is None:
            raise definition_not_found_http(kind=kind, namespace=ns, name=n, version=ver)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"{kind_label} requires id or namespace+name+version.",
        )
    return _definition_item(row, item_cls=item_cls, include_body=include_body)


async def _get_definition_by_path_id(
    *,
    kind: CatalogKind,
    item_cls: type[ItemT],
    get_by_uuid: GetByUuidFn,
    definition_id: str,
    include_body: bool,
) -> ItemT:
    uid = validate_definition_id(definition_id)
    row = await get_by_uuid(definition_id=uid)
    if row is None:
        raise definition_not_found_http(kind=kind, definition_id=definition_id)
    return _definition_item(row, item_cls=item_cls, include_body=include_body)


async def _patch_definition_active(
    *,
    kind: CatalogKind,
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
    uuid_raw, triple = _resolve_identity(
        kind_label=f"{kind_label} PATCH",
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
    )
    if uuid_raw is not None:
        uid = validate_definition_id(uuid_raw)
        row = await get_by_uuid(definition_id=uid)
        if row is None:
            raise definition_not_found_http(kind=kind, definition_id=uuid_raw)
    elif triple is not None:
        ns, n, ver = triple
        validate_namespace(ns)
        row = await get_by_triple(namespace=ns, name=n, version=ver)
        if row is None:
            raise definition_not_found_http(kind=kind, namespace=ns, name=n, version=ver)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"{kind_label} PATCH requires id= or namespace+name+version query parameters.",
        )
    row = await _set_definition_active(row=row, is_active=payload.is_active)
    return _definition_item(row, item_cls=item_cls, include_body=False)


async def _collection_get(
    *,
    kind: CatalogKind,
    kind_label: str,
    item_cls: type[ItemT],
    list_response_cls: type[ListT],
    list_fn: ListFn,
    count_fn: CountFn,
    get_by_uuid: GetByUuidFn,
    get_by_triple: GetByTripleFn,
    definition_id: str | None,
    namespace: str | None,
    name: str | None,
    version: str | None,
    is_active: bool | None,
    include_body: bool,
    include_total: bool,
    limit: int | None,
    offset: int | None,
) -> ItemT | ListT:
    if _wants_identity_get(definition_id=definition_id, version=version):
        return await _get_definition(
            kind=kind,
            kind_label=kind_label,
            item_cls=item_cls,
            get_by_uuid=get_by_uuid,
            get_by_triple=get_by_triple,
            definition_id=definition_id,
            namespace=namespace,
            name=name,
            version=version,
            include_body=include_body,
        )
    return await _list_definitions(
        item_cls=item_cls,
        list_response_cls=list_response_cls,
        list_fn=list_fn,
        count_fn=count_fn,
        namespace=namespace,
        name=name,
        is_active=is_active,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


# --- sagas -----------------------------------------------------------------


@router.get(
    "/sagas",
    response_model=SagaDefinitionItem | SagaDefinitionListResponse,
    response_model_exclude_none=True,
)
async def get_definitions_sagas(
    definition_id: str | None = Query(
        default=None,
        alias="id",
        description="Definition UUID for single-item fetch (XOR with triple).",
    ),
    namespace: str | None = Query(default=None, description="Filter or triple identity."),
    name: str | None = Query(default=None, description="Exact name filter or triple identity."),
    version: str | None = Query(
        default=None,
        description="With namespace+name: single-item fetch by triple.",
    ),
    is_active: bool | None = Query(default=None, description="Filter by active flag (list mode)."),
    include_body: bool = Query(
        default=False,
        description="When fetching one definition, include full manifest body.",
    ),
    include_total: bool = Query(default=False, description="Include total matching row count."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> SagaDefinitionItem | SagaDefinitionListResponse:
    """List saga definitions, or fetch one by ``id`` XOR ``namespace+name+version``."""
    return await _collection_get(
        kind="saga",
        kind_label="Saga",
        item_cls=SagaDefinitionItem,
        list_response_cls=SagaDefinitionListResponse,
        list_fn=read_queries.list_saga_definitions,
        count_fn=read_queries.count_saga_definitions,
        get_by_uuid=read_queries.get_saga_definition_by_uuid,
        get_by_triple=read_queries.get_saga_definition_by_triple,
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
        is_active=is_active,
        include_body=include_body,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


@router.patch("/sagas", response_model=SagaDefinitionItem, response_model_exclude_none=True)
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
        kind="saga",
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


@router.get(
    "/sagas/{definition_id}",
    response_model=SagaDefinitionItem,
    response_model_exclude_none=True,
)
async def get_definitions_saga_by_id(
    definition_id: str,
    include_body: bool = Query(
        default=False,
        description="When true, include the full manifest blueprint in body.",
    ),
) -> SagaDefinitionItem:
    """Return one saga definition by primary key UUID (for start-saga resolution)."""
    return await _get_definition_by_path_id(
        kind="saga",
        item_cls=SagaDefinitionItem,
        get_by_uuid=read_queries.get_saga_definition_by_uuid,
        definition_id=definition_id,
        include_body=include_body,
    )


# --- workers ---------------------------------------------------------------


@router.get(
    "/workers",
    response_model=WorkerDefinitionItem | WorkerDefinitionListResponse,
    response_model_exclude_none=True,
)
async def get_definitions_workers(
    definition_id: str | None = Query(
        default=None,
        alias="id",
        description="Definition UUID for single-item fetch (XOR with triple).",
    ),
    namespace: str | None = Query(default=None, description="Filter or triple identity."),
    name: str | None = Query(default=None, description="Exact name filter or triple identity."),
    version: str | None = Query(
        default=None,
        description="With namespace+name: single-item fetch by triple.",
    ),
    is_active: bool | None = Query(default=None, description="Filter by active flag (list mode)."),
    include_body: bool = Query(
        default=False,
        description="When fetching one definition, include full manifest body.",
    ),
    include_total: bool = Query(default=False, description="Include total matching row count."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> WorkerDefinitionItem | WorkerDefinitionListResponse:
    """List worker definitions, or fetch one by ``id`` XOR ``namespace+name+version``."""
    return await _collection_get(
        kind="worker",
        kind_label="Worker",
        item_cls=WorkerDefinitionItem,
        list_response_cls=WorkerDefinitionListResponse,
        list_fn=read_queries.list_worker_definitions,
        count_fn=read_queries.count_worker_definitions,
        get_by_uuid=read_queries.get_worker_definition_by_uuid,
        get_by_triple=read_queries.get_worker_definition_by_triple,
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
        is_active=is_active,
        include_body=include_body,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


@router.patch("/workers", response_model=WorkerDefinitionItem, response_model_exclude_none=True)
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
        kind="worker",
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


@router.get(
    "/workers/{definition_id}",
    response_model=WorkerDefinitionItem,
    response_model_exclude_none=True,
)
async def get_definitions_worker_by_id(
    definition_id: str,
    include_body: bool = Query(
        default=False,
        description="When true, include the full manifest body.",
    ),
) -> WorkerDefinitionItem:
    """Return one worker definition by primary key UUID."""
    return await _get_definition_by_path_id(
        kind="worker",
        item_cls=WorkerDefinitionItem,
        get_by_uuid=read_queries.get_worker_definition_by_uuid,
        definition_id=definition_id,
        include_body=include_body,
    )


# --- steps -----------------------------------------------------------------


@router.get(
    "/steps",
    response_model=StepDefinitionItem | StepDefinitionListResponse,
    response_model_exclude_none=True,
)
async def get_definitions_steps(
    definition_id: str | None = Query(
        default=None,
        alias="id",
        description="Definition UUID for single-item fetch (XOR with triple).",
    ),
    namespace: str | None = Query(default=None, description="Filter or triple identity."),
    name: str | None = Query(default=None, description="Exact name filter or triple identity."),
    version: str | None = Query(
        default=None,
        description="With namespace+name: single-item fetch by triple.",
    ),
    is_active: bool | None = Query(default=None, description="Filter by active flag (list mode)."),
    include_body: bool = Query(
        default=False,
        description="When fetching one definition, include full manifest body.",
    ),
    include_total: bool = Query(default=False, description="Include total matching row count."),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> StepDefinitionItem | StepDefinitionListResponse:
    """List step definitions, or fetch one by ``id`` XOR ``namespace+name+version``."""
    return await _collection_get(
        kind="step",
        kind_label="Step",
        item_cls=StepDefinitionItem,
        list_response_cls=StepDefinitionListResponse,
        list_fn=read_queries.list_step_definitions,
        count_fn=read_queries.count_step_definitions,
        get_by_uuid=read_queries.get_step_definition_by_uuid,
        get_by_triple=read_queries.get_step_definition_by_triple,
        definition_id=definition_id,
        namespace=namespace,
        name=name,
        version=version,
        is_active=is_active,
        include_body=include_body,
        include_total=include_total,
        limit=limit,
        offset=offset,
    )


@router.patch("/steps", response_model=StepDefinitionItem, response_model_exclude_none=True)
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
        kind="step",
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


@router.get(
    "/steps/{definition_id}",
    response_model=StepDefinitionItem,
    response_model_exclude_none=True,
)
async def get_definitions_step_by_id(
    definition_id: str,
    include_body: bool = Query(
        default=False,
        description="When true, include the full step manifest blueprint in body.",
    ),
) -> StepDefinitionItem:
    """Return one step definition by primary key UUID."""
    return await _get_definition_by_path_id(
        kind="step",
        item_cls=StepDefinitionItem,
        get_by_uuid=read_queries.get_step_definition_by_uuid,
        definition_id=definition_id,
        include_body=include_body,
    )
