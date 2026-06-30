"""Path and query identifier validation for engine read APIs."""

from __future__ import annotations

import re

from fastapi import HTTPException

_TRACE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_STEP_SPAN_ID_RE = re.compile(r"^[a-f0-9]{16}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9-]+$")


def validate_trace_id(trace_id: str) -> None:
    """Raise HTTP 422 when trace_id is not a 32-char lowercase hex saga id."""
    if not _TRACE_ID_RE.match(trace_id):
        raise HTTPException(
            status_code=422,
            detail="trace_id must be a 32-character lowercase hex string.",
        )


def validate_step_span_id(step_span_id: str) -> None:
    """Raise HTTP 422 when step_span_id is not a 16-char lowercase hex span id."""
    if not _STEP_SPAN_ID_RE.match(step_span_id):
        raise HTTPException(
            status_code=422,
            detail="step_span_id must be a 16-character lowercase hex string.",
        )


def validate_namespace(namespace: str) -> None:
    """Raise HTTP 422 when namespace does not match audit envelope rules."""
    if not _NAMESPACE_RE.match(namespace):
        raise HTTPException(
            status_code=422,
            detail="namespace must match ^[a-z0-9-]+$.",
        )
