"""Kernel execution scope for worker/tool hook boundaries (no audit schema)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient


@dataclass(frozen=True)
class ExecutionScope:
    """Worker/command scope passed through hooks; maps to saga_trace_id at ledger boundary."""

    namespace: str
    trace_id: str
    step_span_id: str
    idempotency_key: str
    command_type: str
    worker_name: str
    worker_version: str = ""
    trace_context: dict[str, Any] = field(default_factory=dict)


def execution_scope_from_injection(context: dict[str, Any] | None) -> ExecutionScope | None:
    """Return ExecutionScope from adapter/tool injection context, if present."""
    if not context:
        return None
    raw = context.get("execution_scope")
    if raw is None:
        return None
    if isinstance(raw, ExecutionScope):
        return raw
    if isinstance(raw, dict):
        return ExecutionScope(**raw)
    return None


def db_conn_from_injection(context: dict[str, Any] | None) -> BaseDBAsyncClient | None:
    """Return the active Tortoise connection for hook writes, if present."""
    if not context:
        return None
    conn = context.get("conn")
    return conn if conn is not None else None
