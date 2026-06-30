"""Standard envelope for successful step completion payloads (worker → engine).

Reason steps may emit::

    { "data": <business dict>, "facts": <tool-derived dict> }

Commit steps emit ``{ "data": <MCP JSON object> }`` only.

Downstream JSONPath, CEL, and ``output_schema`` refer to the **inner** business object
(schema validates ``output.data``). Tool-derived ``facts`` are merged into saga context
under ``steps.<step_id>.facts`` and are not LLM-authored.
"""

from __future__ import annotations

from typing import Any

STEP_OUTPUT_DATA_KEY = "data"


def wrap_step_output_data(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap the mergeable business payload for ``STEP_COMPLETED``."""
    return {STEP_OUTPUT_DATA_KEY: data}


def facts_from_step_output(output: dict[str, Any] | None) -> dict[str, Any]:
    """Return tool-derived facts dict from a step output envelope (empty when absent)."""
    if not output or not isinstance(output, dict):
        return {}
    facts = output.get("facts")
    return dict(facts) if isinstance(facts, dict) else {}


def step_context_entry_for_saga(output: dict[str, Any] | None) -> dict[str, Any]:
    """Build ``steps.<id>`` context slice with normalized output data and facts."""
    raw = output if isinstance(output, dict) else {}
    inner = business_data_from_step_output(raw)
    return {
        "output": wrap_step_output_data(dict(inner) if inner is not None else {}),
        "facts": facts_from_step_output(output),
    }


def business_data_from_step_output(output: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the inner ``data`` dict, or ``None`` if missing or not a dict."""
    if not output or not isinstance(output, dict):
        return None
    inner = output.get(STEP_OUTPUT_DATA_KEY)
    return inner if isinstance(inner, dict) else None


def validate_business_data_schema(
    output: dict[str, Any], schema: dict[str, Any], label: str
) -> None:
    """Validate ``output.data`` against ``schema`` (resolved from the saga step's ``output_schema`` path)."""
    from common.governance import validate_against_schema

    inner = business_data_from_step_output(output)
    if inner is None:
        raise ValueError(
            f"{label}: STEP_COMPLETED output must include a '{STEP_OUTPUT_DATA_KEY}' object"
        )
    validate_against_schema(inner, schema, label)
