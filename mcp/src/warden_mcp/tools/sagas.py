"""Saga start and monitoring tools."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastmcp import FastMCP

from warden_mcp.client import EngineClient, get_engine_client
from warden_mcp.errors import EngineAPIError, EngineTransportError, engine_error_result
from warden_mcp.ids import (
    saga_instance_path,
    saga_step_path,
    saga_steps_path,
    validate_step_span_id,
    validate_trace_id,
)
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


async def _fetch_saga(
    client: EngineClient,
    *,
    trace_id: str,
    namespace: str | None,
) -> dict[str, Any] | None:
    """Return saga JSON, None if 404, or raise/propagate other errors as Exception."""
    params = query_params(namespace=namespace)
    path = saga_instance_path(trace_id)
    try:
        return await client.get_json(path, params=params or None)
    except EngineAPIError as exc:
        if exc.status_code == 404:
            return None
        raise


async def _fetch_step_summary(
    client: EngineClient,
    *,
    trace_id: str,
    namespace: str | None,
) -> list[dict[str, Any]] | None:
    params = query_params(namespace=namespace)
    try:
        data = await client.get_json(saga_steps_path(trace_id), params=params or None)
    except (EngineAPIError, EngineTransportError):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    return _slim_steps(items)


async def _wait_for_saga_impl(
    client: EngineClient,
    *,
    trace_id: str,
    namespace: str | None,
    timeout_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        try:
            last = await _fetch_saga(client, trace_id=trace_id, namespace=namespace)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

        if last is None:
            return {
                "done": False,
                "found": False,
                "trace_id": trace_id,
                "error": "No saga found for trace_id",
            }

        status = str(last.get("status", ""))
        if status == "AWAITING_HUMAN" or status in _TERMINAL_SAGA_STATUSES:
            steps = await _fetch_step_summary(client, trace_id=trace_id, namespace=namespace)
            result: dict[str, Any] = {
                "done": True,
                "found": True,
                "status": status,
                "saga": last,
                "trace_id": trace_id,
            }
            if steps is not None:
                result["steps"] = steps
            return result
        await asyncio.sleep(poll_interval_s)

    steps = await _fetch_step_summary(client, trace_id=trace_id, namespace=namespace)
    timed_out: dict[str, Any] = {
        "done": False,
        "timed_out": True,
        "found": last is not None,
        "trace_id": trace_id,
        "saga": last,
        "status": last.get("status") if isinstance(last, dict) else None,
    }
    if steps is not None:
        timed_out["steps"] = steps
    return timed_out


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
        """Poll one saga instance (GET /v1/sagas/{trace_id}).

        Soft-missing: returns found=false on engine 404. Other errors are hard payloads.
        """
        if err := validate_trace_id(trace_id):
            return err

        client = get_engine_client()
        try:
            saga = await _fetch_saga(client, trace_id=trace_id, namespace=namespace)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

        if saga is None:
            return {"found": False, "trace_id": trace_id, "saga": None}
        return {"found": True, "trace_id": trace_id, "saga": saga}

    @mcp.tool
    async def warden_list_saga_steps(
        trace_id: str,
        namespace: str | None = None,
        status: list[str] | None = None,
        include_total: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List runtime step instances for one saga (GET /v1/sagas/{trace_id}/steps).

        Not catalog steps — use warden_list_step_definitions for kind: step manifests.
        """
        if err := validate_trace_id(trace_id):
            return err

        client = get_engine_client()
        params = query_params(
            namespace=namespace,
            include_total=include_total,
            limit=limit,
            offset=offset,
        )
        if status:
            for value in status:
                params.append(("status", value))
        try:
            return await client.get_json(
                saga_steps_path(trace_id),
                params=params or None,
            )
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

        Convenience wrapper around GET /v1/sagas/{trace_id} — not a native engine endpoint.
        Returns immediately with found=false when no saga matches trace_id.
        On done or timeout, includes a slim ``steps`` summary (step_id/status/…);
        use warden_get_step_detail for full payloads.
        """
        if err := validate_trace_id(trace_id):
            return err

        client = get_engine_client()
        return await _wait_for_saga_impl(
            client,
            trace_id=trace_id,
            namespace=namespace,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    @mcp.tool
    async def warden_wait_for_review(
        trace_id: str | None = None,
        namespace: str | None = None,
        timeout_s: float = 60.0,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll until a HITL review is pending, or timeout.

        With ``trace_id``: wait until that saga reaches AWAITING_HUMAN (or terminal —
        then done=false with status). Without ``trace_id``: poll GET /v1/sagas/pending-review
        until items is non-empty. Same timeout contract as warden_wait_for_saga.
        """
        if trace_id is not None:
            if err := validate_trace_id(trace_id):
                return err

        client = get_engine_client()
        deadline = time.monotonic() + timeout_s

        if trace_id is not None:
            last_saga: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                try:
                    last_saga = await _fetch_saga(client, trace_id=trace_id, namespace=namespace)
                except (EngineAPIError, EngineTransportError) as exc:
                    return engine_error_result(exc)

                if last_saga is None:
                    return {
                        "done": False,
                        "found": False,
                        "trace_id": trace_id,
                        "error": "No saga found for trace_id",
                    }

                status = str(last_saga.get("status", ""))
                if status == "AWAITING_HUMAN":
                    steps = await _fetch_step_summary(
                        client, trace_id=trace_id, namespace=namespace
                    )
                    out: dict[str, Any] = {
                        "done": True,
                        "found": True,
                        "status": status,
                        "saga": last_saga,
                        "trace_id": trace_id,
                    }
                    if steps is not None:
                        out["steps"] = steps
                    return out
                if status in _TERMINAL_SAGA_STATUSES:
                    steps = await _fetch_step_summary(
                        client, trace_id=trace_id, namespace=namespace
                    )
                    terminal: dict[str, Any] = {
                        "done": False,
                        "found": True,
                        "status": status,
                        "saga": last_saga,
                        "trace_id": trace_id,
                        "error": "Saga reached terminal status without AWAITING_HUMAN",
                    }
                    if steps is not None:
                        terminal["steps"] = steps
                    return terminal
                await asyncio.sleep(poll_interval_s)

            timed: dict[str, Any] = {
                "done": False,
                "timed_out": True,
                "found": last_saga is not None,
                "trace_id": trace_id,
                "saga": last_saga,
                "status": last_saga.get("status") if isinstance(last_saga, dict) else None,
            }
            return timed

        last_review: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            params = query_params(namespace=namespace, limit=50, offset=0)
            try:
                last_review = await client.get_json(
                    "/v1/sagas/pending-review",
                    params=params or None,
                )
            except (EngineAPIError, EngineTransportError) as exc:
                return engine_error_result(exc)

            items = last_review.get("items") if isinstance(last_review, dict) else None
            if isinstance(items, list) and items:
                return {
                    "done": True,
                    "found": True,
                    "pending_review": last_review,
                    "count": len(items),
                }
            await asyncio.sleep(poll_interval_s)

        items = last_review.get("items") if isinstance(last_review, dict) else None
        return {
            "done": False,
            "timed_out": True,
            "found": False,
            "pending_review": last_review,
            "count": len(items) if isinstance(items, list) else 0,
        }

    @mcp.tool
    async def warden_start_and_wait(
        name: str,
        version: str,
        namespace: str = "default",
        saga_input: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout_s: float = 60.0,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Start a saga then wait until terminal, AWAITING_HUMAN, or timeout.

        Composes warden_start_saga + warden_wait_for_saga with the same bounds.
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
            started = await client.post_json("/v1/sagas/start", json_body=body)
        except (EngineAPIError, EngineTransportError) as exc:
            return engine_error_result(exc)

        trace_id = started.get("trace_id")
        if not isinstance(trace_id, str):
            return {
                "error": True,
                "kind": "validation",
                "detail": "Start response missing trace_id.",
                "start": started,
            }

        waited = await _wait_for_saga_impl(
            client,
            trace_id=trace_id,
            namespace=namespace,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        return {
            "accepted": True,
            "created": started.get("created"),
            "trace_id": trace_id,
            **waited,
        }
