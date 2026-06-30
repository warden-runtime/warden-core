"""Load saga step rows and merge slim outbox allowlists with persisted specs."""

from __future__ import annotations

from typing import Any

from common.models import SagaInstance, SagaStepInstance


async def load_forward_step(
    *,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
) -> SagaStepInstance:
    """Load a forward (non-compensation) step row for worker command hydration."""
    step = await SagaStepInstance.get_or_none(
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        span_id=step_span_id,
    )
    if step is None:
        raise ValueError(
            f"Saga step not found: namespace={namespace!r} trace={saga_trace_id!r} span={step_span_id!r}"
        )
    return step


async def load_saga_instance(*, namespace: str, saga_trace_id: str) -> SagaInstance:
    saga = await SagaInstance.get_or_none(namespace=namespace, trace_id=saga_trace_id)
    if saga is None:
        raise ValueError(f"Saga not found: namespace={namespace!r} trace_id={saga_trace_id!r}")
    return saga


def _tool_specs_by_name(specs: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for spec in specs or []:
        if isinstance(spec, dict) and spec.get("name"):
            by_name[str(spec["name"])] = spec
    return by_name


def merge_tool_specs(
    slim_specs: list[dict[str, Any]] | None,
    full_specs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Merge outbox name-only tool entries with full specs from the step row (order preserved)."""
    full_by_name = _tool_specs_by_name(full_specs)
    merged = [
        full_by_name.get(str(slim["name"]), {"name": str(slim["name"])})
        for slim in (slim_specs or [])
        if isinstance(slim, dict) and slim.get("name")
    ]
    return merged or [s for s in (full_specs or []) if isinstance(s, dict)]


def merge_resource_specs(
    wire_specs: list[Any] | None,
    full_specs: list[Any] | None,
) -> list[dict[str, Any]]:
    """Prefer wire resource allowlist; fall back to step row."""
    if wire_specs:
        return [s if isinstance(s, dict) else {"uri": str(s)} for s in wire_specs]
    return [s if isinstance(s, dict) else {"uri": str(s)} for s in (full_specs or [])]
