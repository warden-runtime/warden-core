"""Compensation eligibility helpers (kernel-safe; no enterprise imports)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.models import SagaStepInstance

from common.models import StepStatus


def forward_step_has_compensation(forward: SagaStepInstance) -> bool:
    """True when the forward step needs compensation handling on failure unwind.

    ``spawn_sagas`` always participates (engine awaits children). ``join_sagas`` is a
    no-op skip. Worker steps use the declared compensation block when present.
    """
    if forward.step_kind == "spawn_sagas":
        return True
    if forward.step_kind == "join_sagas":
        return False
    return forward.compensation_definition is not None


def forward_eligible_for_compensation(forward: SagaStepInstance) -> bool:
    """Forward rows that may have committed partial external effects."""
    return forward.status in {
        StepStatus.COMPLETED,
        StepStatus.FAILED,
        StepStatus.TIMED_OUT,
        StepStatus.IN_PROGRESS,
    }


def _normalize_spec_list(items: object, *, fallback_key: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [s if isinstance(s, dict) else {fallback_key: str(s)} for s in items]


def compensation_tool_resource_specs(
    comp_def: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    comp_tools = comp_def.get("tools") or {}
    tool_allow = comp_tools.get("allow", []) if isinstance(comp_tools, dict) else []
    comp_resources = comp_def.get("resources") or {}
    resource_allow = comp_resources.get("allow", []) if isinstance(comp_resources, dict) else []
    return (
        _normalize_spec_list(tool_allow, fallback_key="name"),
        _normalize_spec_list(resource_allow, fallback_key="uri"),
    )
