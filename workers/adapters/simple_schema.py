"""Fallback JSON Schema for reason steps using agent-adapter: simple without output_schema."""

from __future__ import annotations

from typing import Any

FALLBACK_SIMPLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "The unstructured text output of the step.",
        }
    },
    "required": ["summary"],
    "additionalProperties": False,
}


def resolve_effective_schema(output_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Return explicit step schema or the built-in summary fallback."""
    if output_schema:
        return output_schema
    return dict(FALLBACK_SIMPLE_OUTPUT_SCHEMA)
