"""Human-in-the-loop review tools."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from warden_mcp.client import get_engine_client
from warden_mcp.errors import EngineAPIError, EngineTransportError, engine_error_result
from warden_mcp.ids import saga_step_path, validate_step_span_id, validate_trace_id
from warden_mcp.tools._params import query_params


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def warden_list_pending_reviews(
        trace_id: str | None = None,
        namespace: str | None = None,
        kind: Literal["reason", "commit"] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List HITL-held steps awaiting human review (GET /v1/sagas/pending-review)."""
        if trace_id is not None:
            if err := validate_trace_id(trace_id):
                return err

        client = get_engine_client()
        params = query_params(
            trace_id=trace_id,
            namespace=namespace,
            kind=kind,
            limit=limit,
            offset=offset,
        )
        try:
            return await client.get_json("/v1/sagas/pending-review", params=params or None)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_decide_step(
        trace_id: str,
        step_span_id: str,
        decision: Literal["APPROVE", "REJECT"],
        namespace: str = "default",
        output: dict[str, Any] | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a HITL decision (POST /v1/sagas/.../decision). Returns HTTP 202."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {"decision": decision}
        if output is not None:
            body["output"] = output
        if error_details is not None:
            body["error_details"] = error_details

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/decision"
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
    async def warden_approve_step(
        trace_id: str,
        step_span_id: str,
        namespace: str = "default",
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Approve a HITL-held step (POST /v1/sagas/.../approve). Returns HTTP 202."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {}
        if output is not None:
            body["output"] = output

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/approve"
        try:
            data = await client.post_json(
                path,
                json_body=body or None,
                params=[("namespace", namespace)],
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}

    @mcp.tool
    async def warden_reject_step(
        trace_id: str,
        step_span_id: str,
        namespace: str = "default",
        error_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reject a HITL-held step (POST /v1/sagas/.../reject). Returns HTTP 202."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {}
        if error_details is not None:
            body["error_details"] = error_details

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/reject"
        try:
            data = await client.post_json(
                path,
                json_body=body or None,
                params=[("namespace", namespace)],
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}

    @mcp.tool
    async def warden_retry_hitl_step(
        trace_id: str,
        step_span_id: str,
        namespace: str = "default",
        guidance: str | None = None,
        retry_token: str | None = None,
    ) -> dict[str, Any]:
        """Re-run a HITL-held step with optional operator guidance (POST .../retry)."""
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        body: dict[str, Any] = {}
        if guidance is not None:
            body["guidance"] = guidance
        if retry_token is not None:
            body["retry_token"] = retry_token

        client = get_engine_client()
        path = f"{saga_step_path(trace_id, step_span_id)}/retry"
        try:
            data = await client.post_json(
                path,
                json_body=body or None,
                params=[("namespace", namespace)],
            )
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}
