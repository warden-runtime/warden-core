"""Manifest deployment tools."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from warden_mcp.client import get_engine_client
from warden_mcp.config import get_settings
from warden_mcp.errors import (
    EngineAPIError,
    EngineTransportError,
    engine_error_result,
    tool_validation_error,
)


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def warden_deploy_manifest(
        yaml_content: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Register a worker, step, or saga manifest with the engine (POST /v1/manifests).

        Provide manifest YAML as yaml_content. Returns synchronously on success (HTTP 200).
        Set dry_run=true to validate and link-check without persisting
        (POST /v1/manifests?dry_run=true).

        Deploy order for first-class steps: workers → steps → sagas. Saga manifests
        compose catalog steps via use:/version; deploy referenced workers and steps first.
        """
        if not yaml_content or not yaml_content.strip():
            return tool_validation_error("Provide non-empty yaml_content.")

        content_bytes = yaml_content.encode("utf-8")
        max_bytes = get_settings().manifest_max_body_bytes
        if len(content_bytes) > max_bytes:
            return tool_validation_error(
                f"Manifest exceeds limit of {max_bytes} bytes.",
            )

        client = get_engine_client()
        params = [("dry_run", "true")] if dry_run else None
        try:
            data = await client.post_json(
                "/v1/manifests",
                content=content_bytes,
                headers={"Content-Type": "application/x-yaml"},
                params=params,
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"ok": True, **data}
