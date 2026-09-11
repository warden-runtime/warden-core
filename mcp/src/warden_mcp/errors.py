"""Engine API error types."""

from __future__ import annotations

import json
from typing import Any


class EngineAPIError(Exception):
    """Raised when the Warden engine returns a non-success HTTP status."""

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        path: str,
        detail: str,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        self.detail = detail
        self.fields = fields or {}
        super().__init__(f"engine HTTP {status_code} {method} {path}: {detail}")


class EngineTransportError(Exception):
    """Raised when the engine cannot be reached or returns an unparseable response."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        detail: str,
    ) -> None:
        self.method = method
        self.path = path
        self.detail = detail
        super().__init__(f"engine transport error {method} {path}: {detail}")


def parse_engine_error_body(
    response_body: str,
    *,
    status_code: int,
) -> tuple[str, dict[str, Any]]:
    """Return (human-readable detail, structured fields) from an engine error body.

    Catalog errors use ``detail`` as an object with ``code``, ``kind``, identity
    fields, and ``message``. Those are promoted onto the tool error payload as
    ``code``, ``catalog_kind``, ``namespace``, ``name``, ``version``.
    """
    try:
        data = json.loads(response_body)
    except (json.JSONDecodeError, TypeError):
        body = (response_body or "").strip().replace("\n", " ")[:400]
        return body or f"HTTP {status_code}", {}

    detail = data.get("detail")
    if isinstance(detail, str):
        return detail, {}
    if isinstance(detail, dict) and detail.get("code"):
        fields: dict[str, Any] = {"code": detail["code"]}
        if "kind" in detail:
            fields["catalog_kind"] = detail["kind"]
        for key in ("namespace", "name", "version"):
            if key in detail:
                fields[key] = detail[key]
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message, fields
        return json.dumps(detail, default=str)[:500], fields
    if detail is not None:
        return json.dumps(detail, default=str)[:500], {}
    body = (response_body or "").strip().replace("\n", " ")[:400]
    return body or f"HTTP {status_code}", {}


def format_api_detail(response_body: str, *, status_code: int) -> str:
    """Extract a human-readable error from an engine error response."""
    message, _fields = parse_engine_error_body(response_body, status_code=status_code)
    return message


def engine_error_payload(exc: EngineAPIError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": True,
        "kind": "http",
        "status_code": exc.status_code,
        "method": exc.method,
        "path": exc.path,
        "detail": exc.detail,
    }
    payload.update(exc.fields)
    return payload


def transport_error_payload(exc: EngineTransportError) -> dict[str, Any]:
    return {
        "error": True,
        "kind": "transport",
        "method": exc.method,
        "path": exc.path,
        "detail": exc.detail,
    }


def tool_validation_error(detail: str) -> dict[str, Any]:
    return {
        "error": True,
        "kind": "validation",
        "detail": detail,
    }


def engine_error_result(exc: EngineAPIError | EngineTransportError) -> dict[str, Any]:
    if isinstance(exc, EngineAPIError):
        return engine_error_payload(exc)
    return transport_error_payload(exc)
