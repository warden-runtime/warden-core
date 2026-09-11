"""Saga start and monitoring tools."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastmcp import FastMCP

from warden_mcp.client import EngineClient, get_engine_client
from warden_mcp.errors import EngineAPIError, EngineTransportError, engine_error_result
from warden_mcp.ids import saga_step_path, validate_step_span_id, validate_trace_id
from warden_mcp.tools._params import query_params

_TERMINAL_SAGA_STATUSES = frozenset({"COMPLETED", "FAILED", "COMPENSATED", "CANCELLED", "REJECTED"})


def _slim_steps(items: list[Any]) -> list[dict[str, Any]]:
    """Compact runtime step rows for wait helpers (status triage, not full detail)."""
    slim: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {
            "step_id": raw.get("step_id"),
            "step_span_id": raw.get("step_span_id"),
            "status": raw.get("status"),
            "step_kind": raw.get("step_kind"),
            "order_index": raw.get("order_index"),
        }
        if raw.get("error_details") is not None:
            entry["error_details"] = raw["error_details"]
        slim.append(entry)
    return slim


async def _fetch_step_summary(
    client: EngineClient,
    *,
    trace_id: str,
    namespace: str | None,
) -> list[dict[str, Any]] | None:
    params = query_params(trace_id=trace_id, namespace=namespace)
    try:
        data = await client.get_json("/v1/sagas/steps", params=params)
    except (EngineAPIError, EngineTransportError):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    return _slim_steps(items)


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def warden_start_saga(
        name: str,
        version: str,
        namespace: str = "default",
        saga_input: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Start a saga instance from a registered definition (POST /v1/sagas/start).

        Returns HTTP 202 with trace_id and created (false on idempotent replay).
        Start hydrates catalog use: refs and freezes prompts/policies/skills onto the
        instance — missing/inactive catalog pins or freeze failures fail before accept.
        Poll with warden_get_saga_status or warden_list_saga_steps (runtime instances).
        """
        body: dict[str, Any] = {
            "namespace": namespace,
            "name": name,
            "version": version,
            "input": saga_input or {},
        }
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key

        client = get_engine_client()
        try:
            data = await client.post_json("/v1/sagas/start", json_body=body)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)
        return {"accepted": True, **data}

    @mcp.tool
    async def warden_list_sagas(
        namespace: str | None = None,
        trace_id: str | None = None,
        parent_trace_id: str | None = None,
        status: list[str] | None = None,
        in_flight: bool | None = None,
        failed: bool | None = None,
        include_total: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List saga instances (GET /v1/sagas).

        Filters: namespace, trace_id, parent_trace_id, status (repeatable),
        in_flight=true, or failed=true. in_flight/failed are mutually exclusive
        with each other and with status (engine returns 400 if combined).
        For a single known trace_id, warden_get_saga_status is a thinner wrapper.
        """
        if trace_id is not None:
            if err := validate_trace_id(trace_id):
                return err
        if parent_trace_id is not None:
            if err := validate_trace_id(parent_trace_id):
                return err

        client = get_engine_client()
        params = query_params(
            namespace=namespace,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            in_flight=in_flight,
            failed=failed,
            include_total=include_total,
            limit=limit,
            offset=offset,
        )
        if status:
            for value in status:
                params.append(("status", value))
        try:
            return await client.get_json("/v1/sagas", params=params or None)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_get_saga_status(
        trace_id: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Poll one saga instance status (GET /v1/sagas?trace_id=...)."""
        if err := validate_trace_id(trace_id):
            return err

        client = get_engine_client()
        params = query_params(trace_id=trace_id, namespace=namespace)
        try:
            data = await client.get_json("/v1/sagas", params=params)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

        items = data.get("items", [])
        if not items:
            return {"found": False, "trace_id": trace_id, "saga": None}
        return {"found": True, "trace_id": trace_id, "saga": items[0]}

    @mcp.tool
    async def warden_list_saga_steps(
        trace_id: str,
        namespace: str | None = None,
        status: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List runtime step instances for one saga (GET /v1/sagas/steps).

        Not catalog steps — use warden_list_step_definitions for kind: step manifests.
        """
        if err := validate_trace_id(trace_id):
            return err

        client = get_engine_client()
        params = query_params(trace_id=trace_id, namespace=namespace, limit=limit, offset=offset)
        if status:
            for value in status:
                params.append(("status", value))
        try:
            return await client.get_json("/v1/sagas/steps", params=params)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_get_step_detail(
        trace_id: str,
        step_span_id: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one runtime step with resolved inputs and output.

        GET /v1/sagas/{trace_id}/steps/{step_span_id}. For catalog step definitions,
        use warden_get_step_definition instead.
        """
        if err := validate_trace_id(trace_id):
            return err
        if err := validate_step_span_id(step_span_id):
            return err

        client = get_engine_client()
        params = query_params(namespace=namespace)
        path = saga_step_path(trace_id, step_span_id)
        try:
            return await client.get_json(path, params=params or None)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

    @mcp.tool
    async def warden_wait_for_saga(
        trace_id: str,
        namespace: str | None = None,
        timeout_s: float = 60.0,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll saga status until terminal, AWAITING_HUMAN, or timeout.

        Convenience wrapper around GET /v1/sagas — not a native engine endpoint.
        Returns immediately with found=false when no saga matches trace_id.
        On done or timeout, includes a slim ``steps`` summary (step_id/status/…);
        use warden_get_step_detail for full payloads.
        """
        if err := validate_trace_id(trace_id):
            return err

        client = get_engine_client()
        params = query_params(trace_id=trace_id, namespace=namespace)
        deadline = time.monotonic() + timeout_s

        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                last = await client.get_json("/v1/sagas", params=params)
            except (EngineAPIError, EngineTransportError) as exc:
                return engine_error_result(exc)

            items = last.get("items", [])
            if not items:
                return {
                    "done": False,
                    "found": False,
                    "trace_id": trace_id,
                    "error": "No saga found for trace_id",
                }

            saga = items[0]
            status = str(saga.get("status", ""))
            if status == "AWAITING_HUMAN" or status in _TERMINAL_SAGA_STATUSES:
                steps = await _fetch_step_summary(client, trace_id=trace_id, namespace=namespace)
                result: dict[str, Any] = {
                    "done": True,
                    "found": True,
                    "status": status,
                    "saga": saga,
                    "trace_id": trace_id,
                }
                if steps is not None:
                    result["steps"] = steps
                return result
            await asyncio.sleep(poll_interval_s)

        saga = None
        if last is not None:
            last_items = last.get("items") or []
            if last_items:
                saga = last_items[0]
        steps = await _fetch_step_summary(client, trace_id=trace_id, namespace=namespace)
        timed_out: dict[str, Any] = {
            "done": False,
            "timed_out": True,
            "found": True,
            "trace_id": trace_id,
            "saga": saga,
            "status": saga.get("status") if isinstance(saga, dict) else None,
        }
        if steps is not None:
            timed_out["steps"] = steps
        return timed_out
