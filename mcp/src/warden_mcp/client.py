"""Async HTTP client for the Warden engine API."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx

from warden_mcp.config import Settings, get_settings
from warden_mcp.errors import EngineAPIError, EngineTransportError, parse_engine_error_body

_shared_http: httpx.AsyncClient | None = None


async def open_shared_http_client(settings: Settings | None = None) -> httpx.AsyncClient:
    """Create or return the process-wide httpx client (used during MCP server lifespan)."""
    global _shared_http
    if _shared_http is None:
        s = settings or get_settings()
        _shared_http = httpx.AsyncClient(timeout=httpx.Timeout(s.http_timeout_s))
    return _shared_http


async def close_shared_http_client() -> None:
    """Close the shared httpx client if open."""
    global _shared_http
    if _shared_http is not None:
        await _shared_http.aclose()
        _shared_http = None


def get_engine_client(settings: Settings | None = None) -> EngineClient:
    """Return an EngineClient; uses the shared httpx client when the MCP lifespan is active."""
    return EngineClient(settings=settings)


class EngineClient:
    """Thin httpx wrapper over the engine /v1 API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.engine_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    async def request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: list[tuple[str, str]] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        timeout = httpx.Timeout(self._settings.http_timeout_s)
        try:
            shared = _shared_http
            if shared is not None:
                response = await shared.request(
                    method,
                    url,
                    content=content,
                    json=json_body,
                    headers=headers,
                    params=cast("Any", params),
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        content=content,
                        json=json_body,
                        headers=headers,
                        params=cast("Any", params),
                    )
        except httpx.HTTPError as exc:
            raise EngineTransportError(
                method=method,
                path=path,
                detail=str(exc),
            ) from exc
        if response.is_error:
            detail, fields = parse_engine_error_body(
                response.text,
                status_code=response.status_code,
            )
            raise EngineAPIError(
                status_code=response.status_code,
                method=method,
                path=path,
                detail=detail,
                fields=fields,
            )
        return response

    async def get_json(
        self,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        response = await self.request("GET", path, params=params)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise EngineTransportError(
                method="GET",
                path=path,
                detail=f"Invalid JSON response: {exc}",
            ) from exc
        if not isinstance(data, dict):
            msg = f"Expected JSON object from GET {path}, got {type(data).__name__}"
            raise EngineTransportError(method="GET", path=path, detail=msg)
        return data

    async def post_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        response = await self.request(
            "POST",
            path,
            json_body=json_body,
            content=content,
            headers=headers,
            params=params,
        )
        if not response.content:
            return {}
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise EngineTransportError(
                method="POST",
                path=path,
                detail=f"Invalid JSON response: {exc}",
            ) from exc
        if not isinstance(data, dict):
            msg = f"Expected JSON object from POST {path}, got {type(data).__name__}"
            raise EngineTransportError(method="POST", path=path, detail=msg)
        return data

    async def patch_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        response = await self.request(
            "PATCH",
            path,
            json_body=json_body,
            params=params,
        )
        if not response.content:
            return {}
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise EngineTransportError(
                method="PATCH",
                path=path,
                detail=f"Invalid JSON response: {exc}",
            ) from exc
        if not isinstance(data, dict):
            msg = f"Expected JSON object from PATCH {path}, got {type(data).__name__}"
            raise EngineTransportError(method="PATCH", path=path, detail=msg)
        return data
