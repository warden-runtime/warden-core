"""Path and query identifier validation for MCP tools."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal
from urllib.parse import quote

from warden_mcp.errors import tool_validation_error

_TRACE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_STEP_SPAN_ID_RE = re.compile(r"^[a-f0-9]{16}$")

DefinitionKind = Literal["workers", "steps", "sagas"]
_DEFINITION_KINDS = frozenset({"workers", "steps", "sagas"})


def validate_trace_id(trace_id: str) -> dict[str, Any] | None:
    if not _TRACE_ID_RE.match(trace_id):
        return tool_validation_error(
            "trace_id must be a 32-character lowercase hex string.",
        )
    return None


def validate_step_span_id(step_span_id: str) -> dict[str, Any] | None:
    if not _STEP_SPAN_ID_RE.match(step_span_id):
        return tool_validation_error(
            "step_span_id must be a 16-character lowercase hex string.",
        )
    return None


def validate_definition_id(definition_id: str) -> dict[str, Any] | None:
    try:
        uuid.UUID(definition_id.strip())
    except ValueError:
        return tool_validation_error("definition_id must be a valid UUID.")
    return None


def validate_definition_kind(kind: str) -> dict[str, Any] | None:
    if kind not in _DEFINITION_KINDS:
        return tool_validation_error(
            "kind must be one of: workers, steps, sagas.",
        )
    return None


def resolve_definition_identity_params(
    *,
    definition_id: str | None,
    namespace: str | None,
    name: str | None,
    version: str | None,
) -> dict[str, Any] | list[tuple[str, str]]:
    """Return query params for PATCH identity, or a validation error dict.

    Requires ``definition_id`` XOR ``namespace+name+version`` (all three).
    """
    id_raw = (definition_id or "").strip() or None
    ns = (namespace or "").strip() or None
    n = (name or "").strip() or None
    ver = (version or "").strip() or None

    has_id = id_raw is not None
    has_triple = ns is not None and n is not None and ver is not None
    partial_triple = (ns is not None or n is not None or ver is not None) and not has_triple

    if has_id and (has_triple or partial_triple):
        return tool_validation_error(
            "Provide either definition_id or namespace+name+version, not both.",
        )
    if partial_triple:
        return tool_validation_error(
            "Triple identity requires namespace, name, and version together.",
        )
    if has_id:
        if err := validate_definition_id(id_raw):
            return err
        return [("id", id_raw)]
    if has_triple:
        return [("namespace", ns), ("name", n), ("version", ver)]
    return tool_validation_error(
        "Provide definition_id or namespace+name+version.",
    )


def saga_step_path(trace_id: str, step_span_id: str) -> str:
    return f"/v1/sagas/{quote(trace_id, safe='')}/steps/{quote(step_span_id, safe='')}"


def saga_definition_path(definition_id: str) -> str:
    return f"/v1/definitions/sagas/{quote(definition_id.strip(), safe='')}"


def worker_definition_path(definition_id: str) -> str:
    return f"/v1/definitions/workers/{quote(definition_id.strip(), safe='')}"


def step_definition_path(definition_id: str) -> str:
    return f"/v1/definitions/steps/{quote(definition_id.strip(), safe='')}"


def definitions_collection_path(kind: DefinitionKind) -> str:
    return f"/v1/definitions/{kind}"
