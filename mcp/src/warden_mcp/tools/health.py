"""Health and connectivity tools."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from warden_mcp.client import get_engine_client
from warden_mcp.errors import EngineAPIError, EngineTransportError, engine_error_result


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def warden_ping() -> dict[str, Any]:
        """Check that the Warden engine process is serving (GET /v1/health).

        Liveness only — does not verify Postgres. Prefer warden_ready before deploy/start.
        """
        client = get_engine_client()
        try:
            data = await client.get_json("/v1/health")
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"ok": True, **data}

    @mcp.tool
    async def warden_ready() -> dict[str, Any]:
        """Check that the engine can reach Postgres (GET /v1/health/ready).

        Returns HTTP 503 (error payload) when the database is unavailable.
        """
        client = get_engine_client()
        try:
            data = await client.get_json("/v1/health/ready")
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"ok": True, **data}
