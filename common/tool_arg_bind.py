"""Saga-wins overlay of reason-step ``tools.bind`` keys onto MCP tool arguments."""

from __future__ import annotations

from typing import Any


def overlay_bound_tool_arguments(
    llm_args: dict[str, Any],
    bound: dict[str, Any],
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Force saga-bound values onto tool args for keys present on the tool schema.

    Bound keys that are not in ``inputSchema.properties`` are ignored (prompt-only).
    Saga values always win over LLM args, including null/empty model values.
    """
    props = (input_schema or {}).get("properties") or {}
    if not isinstance(props, dict) or not bound:
        return dict(llm_args)
    out = dict(llm_args)
    for key, value in bound.items():
        if key in props:
            out[key] = value
    return out


def bound_arguments_from_step(
    arguments: dict[str, Any],
    bind_keys: list[str] | None,
) -> dict[str, Any]:
    """Select resolved ``with`` values for the step's ``tools.bind`` keys."""
    if not bind_keys:
        return {}
    return {key: arguments[key] for key in bind_keys if key in arguments}
