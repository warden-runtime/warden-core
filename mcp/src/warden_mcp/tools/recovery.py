"""Operator recovery tools."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from warden_mcp.client import get_engine_client
from warden_mcp.errors import EngineAPIError, EngineTransportError, engine_error_result
from warden_mcp.ids import saga_step_path, validate_step_span_id, validate_trace_id


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def warden_retry_stuck_step(
        trace_id: str,
        step_span_id: str,
        namespace: str = "default",
        recovery_token: str | None = None,
        force: bool = False,
        allow_destructive: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Retry a stuck forward step in IN_PROGRESS (POST .../retry-step). Returns HTTP 202."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {
            "force": force,
            "allow_destructive": allow_destructive,
        }
        if recovery_token is not None:
            body["recovery_token"] = recovery_token
        if reason is not None:
            body["reason"] = reason

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/retry-step"
        try:
            data = await client.post_json(
                path,
                json_body=body,
                params=[("namespace", namespace)],
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}

    @mcp.tool
    async def warden_retry_compensation(
        trace_id: str,
        step_span_id: str,
        namespace: str = "default",
        recovery_token: str | None = None,
        force: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Retry a failed or stuck compensation step (POST .../retry-compensation)."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {"force": force}
        if recovery_token is not None:
            body["recovery_token"] = recovery_token
        if reason is not None:
            body["reason"] = reason

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/retry-compensation"
        try:
            data = await client.post_json(
                path,
                json_body=body,
                params=[("namespace", namespace)],
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}

    @mcp.tool
    async def warden_retry_forward_step(
        trace_id: str,
        step_span_id: str,
        namespace: str = "default",
        recovery_token: str | None = None,
        force: bool = False,
        allow_destructive: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Retry a failed forward step held at AWAITING_RECOVERY (POST .../retry-forward)."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {
            "force": force,
            "allow_destructive": allow_destructive,
        }
        if recovery_token is not None:
            body["recovery_token"] = recovery_token
        if reason is not None:
            body["reason"] = reason

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/retry-forward"
        try:
            data = await client.post_json(
                path,
                json_body=body,
                params=[("namespace", namespace)],
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}

    @mcp.tool
    async def warden_start_compensation(
        trace_id: str,
        step_span_id: str,
        namespace: str = "default",
        recovery_token: str | None = None,
        force: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Start compensation from AWAITING_RECOVERY (POST .../start-compensation)."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {"force": force}
        if recovery_token is not None:
            body["recovery_token"] = recovery_token
        if reason is not None:
            body["reason"] = reason

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/start-compensation"
        try:
            data = await client.post_json(
                path,
                json_body=body,
                params=[("namespace", namespace)],
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}
