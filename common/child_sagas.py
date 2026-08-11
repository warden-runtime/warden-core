"""Pure helpers for child-saga spawn/join (no engine/DB side effects)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from jsonpath_ng import parse as parse_jsonpath
from jsonpath_ng.exceptions import JsonPathParserError

from common.models import SagaStatus
from common.schemas.saga import MAX_CHILDREN_HARD_CAP

DEFAULT_MAX_CHILDREN = MAX_CHILDREN_HARD_CAP

CHILD_TERMINAL: frozenset[str] = frozenset(
    {
        SagaStatus.COMPLETED.value,
        SagaStatus.FAILED.value,
        SagaStatus.COMPENSATED.value,
    }
)


def child_start_idempotency_key(parent_trace_id: str, spawn_step_id: str, item_id: str) -> str:
    """Deterministic saga-start idempotency key for one spawn item (sha256 hex)."""
    digest = hashlib.sha256(f"{parent_trace_id}:{spawn_step_id}:{item_id}".encode()).hexdigest()
    return digest[:256]


def jsonpath_first(document: Any, path: str) -> Any | None:
    """Return the first JSONPath match value, or None if missing/invalid."""
    try:
        expr = parse_jsonpath(path)
    except JsonPathParserError:
        return None
    matches = expr.find(document)
    if not matches:
        return None
    return matches[0].value


def resolve_items_from_context(items_from: str, context: dict[str, Any]) -> list[Any]:
    """Resolve ``items_from`` against saga context; must be a list (or empty)."""
    value = jsonpath_first(context, items_from)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"items_from {items_from!r} must resolve to a list, got {type(value).__name__}"
        )
    return value


def validate_spawn_items(
    items: Sequence[Any],
    max_children: int | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Validate spawn items; return ``(item_id, item)`` pairs.

    Raises:
        ValueError: message includes ``SPAWN_EMPTY_ITEMS``, ``TOO_MANY_CHILDREN``,
            or ``SPAWN_ITEM_MISSING_ID``.
    """
    cap = DEFAULT_MAX_CHILDREN if max_children is None else max_children
    if not items:
        raise ValueError("SPAWN_EMPTY_ITEMS: items_from resolved to an empty list")
    if len(items) > cap:
        raise ValueError(f"TOO_MANY_CHILDREN: {len(items)} items exceeds max_children={cap}")

    validated: list[tuple[str, dict[str, Any]]] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError(
                f"SPAWN_ITEM_MISSING_ID: item at index {index} must be an object with string id"
            )
        item_id = raw.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError(
                f"SPAWN_ITEM_MISSING_ID: item at index {index} requires a non-empty string id"
            )
        validated.append((item_id.strip(), raw))
    return validated


def build_child_resolve_context(
    parent_context: dict[str, Any],
    item: Any,
    item_var: str = "item",
) -> dict[str, Any]:
    """Parent context enriched with ``item`` and ``{item_var}`` for input bindings."""
    resolve_ctx = dict(parent_context)
    resolve_ctx["item"] = item
    var = (item_var or "item").strip() or "item"
    resolve_ctx[var] = item
    return resolve_ctx


def _row_status(row: Any) -> str:
    if isinstance(row, Mapping):
        status = row.get("status")
    else:
        status = getattr(row, "status", None)
    if status is None:
        return ""
    return str(getattr(status, "value", status))


def summarize_join_children(rows: Sequence[Any]) -> dict[str, int]:
    """Build join ``summary`` counts from child rows (status field / attribute).

    ``succeeded`` counts COMPLETED; ``failed`` counts FAILED + COMPENSATED.
    """
    total = len(rows)
    succeeded = 0
    failed = 0
    for row in rows:
        status = _row_status(row)
        if status == SagaStatus.COMPLETED.value:
            succeeded += 1
        elif status in {SagaStatus.FAILED.value, SagaStatus.COMPENSATED.value}:
            failed += 1
    return {"total": total, "succeeded": succeeded, "failed": failed}


__all__ = [
    "CHILD_TERMINAL",
    "DEFAULT_MAX_CHILDREN",
    "build_child_resolve_context",
    "child_start_idempotency_key",
    "jsonpath_first",
    "resolve_items_from_context",
    "summarize_join_children",
    "validate_spawn_items",
]
