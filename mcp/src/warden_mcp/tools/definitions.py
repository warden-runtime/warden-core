"""Catalog definition tools (workers, steps, sagas)."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from warden_mcp.client import get_engine_client
from warden_mcp.errors import EngineAPIError, EngineTransportError, engine_error_result
from warden_mcp.ids import (
    definitions_collection_path,
    resolve_definition_identity_params,
    saga_definition_path,
    step_definition_path,
    validate_definition_id,
    validate_definition_kind,
    worker_definition_path,
)
from warden_mcp.tools._params import query_params

DefinitionKind = Literal["workers", "steps", "sagas"]


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def warden_list_saga_definitions(
        namespace: str | None = None,
        name: str | None = None,
        is_active: bool | None = None,
        include_total: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List registered saga definitions (GET /v1/definitions/sagas).

        Saga bodies (include_body on get) are authoring ASTs (use:/version/with/when),
        not expanded executable graphs — hydration happens at saga start.
        """
        client = get_engine_client()
        params = query_params(
            namespace=namespace,
            name=name,
            is_active=is_active,
            include_total=include_total,
            limit=limit,
            offset=offset,
        )
        try:
            return await client.get_json("/v1/definitions/sagas", params=params or None)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_list_worker_definitions(
        namespace: str | None = None,
        name: str | None = None,
        is_active: bool | None = None,
        include_total: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List registered worker definitions (GET /v1/definitions/workers)."""
        client = get_engine_client()
        params = query_params(
            namespace=namespace,
            name=name,
            is_active=is_active,
            include_total=include_total,
            limit=limit,
            offset=offset,
        )
        try:
            return await client.get_json("/v1/definitions/workers", params=params or None)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_list_step_definitions(
        namespace: str | None = None,
        name: str | None = None,
        is_active: bool | None = None,
        include_total: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List catalog step definitions (GET /v1/definitions/steps).

        These are reusable capabilities (kind: step). Runtime step instances for a
        running saga are listed with warden_list_saga_steps instead.
        """
        client = get_engine_client()
        params = query_params(
            namespace=namespace,
            name=name,
            is_active=is_active,
            include_total=include_total,
            limit=limit,
            offset=offset,
        )
        try:
            return await client.get_json("/v1/definitions/steps", params=params or None)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_get_saga_definition(
        definition_id: str,
        include_body: bool = False,
    ) -> dict[str, Any]:
        """Fetch one saga definition by UUID (GET /v1/definitions/sagas/{id}).

        With include_body=true, body is the authoring AST (use:/version/with/when),
        not an expanded reason/commit photocopy.
        """
        if err := validate_definition_id(definition_id):
            return err

        client = get_engine_client()
        params = query_params(include_body=include_body)
        try:
            return await client.get_json(
                saga_definition_path(definition_id),
                params=params or None,
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_get_worker_definition(
        definition_id: str,
        include_body: bool = False,
    ) -> dict[str, Any]:
        """Fetch one worker definition by UUID (GET /v1/definitions/workers/{id})."""
        if err := validate_definition_id(definition_id):
            return err

        client = get_engine_client()
        params = query_params(include_body=include_body)
        try:
            return await client.get_json(
                worker_definition_path(definition_id),
                params=params or None,
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_get_step_definition(
        definition_id: str,
        include_body: bool = False,
    ) -> dict[str, Any]:
        """Fetch one catalog step definition by UUID (GET /v1/definitions/steps/{id}).

        Catalog steps (kind: step), not runtime saga step instances.
        """
        if err := validate_definition_id(definition_id):
            return err

        client = get_engine_client()
        params = query_params(include_body=include_body)
        try:
            return await client.get_json(
                step_definition_path(definition_id),
                params=params or None,
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_set_definition_active(
        kind: DefinitionKind,
        is_active: bool,
        definition_id: str | None = None,
        namespace: str | None = None,
        name: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Soft-enable or soft-disable a catalog definition (PATCH /v1/definitions/{kind}).

        kind is workers, steps, or sagas. Identify with definition_id XOR
        namespace+name+version. Inactive pins are rejected at deploy link-check and start.
        """
        if err := validate_definition_kind(kind):
            return err
        identity = resolve_definition_identity_params(
            definition_id=definition_id,
            namespace=namespace,
            name=name,
            version=version,
        )
        if isinstance(identity, dict):
            return identity

        client = get_engine_client()
        path = definitions_collection_path(kind)
        try:
            return await client.patch_json(
                path,
                json_body={"is_active": is_active},
                params=identity,
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
