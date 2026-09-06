"""Tighten-only overrides when hydrating saga ``use:`` refs onto catalog steps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.schemas.saga import SagaStepRef


def _require_hitl_enabled(step_id: str, capability: dict[str, Any], *, field: str) -> None:
    if not capability.get("hitl"):
        raise ValueError(f"step {step_id!r} {field} requires hitl: true after overrides")


def _apply_hitl_overrides(
    *,
    step_id: str,
    out: dict[str, Any],
    ref: SagaStepRef,
) -> None:
    if ref.hitl is not None:
        if ref.hitl is False and bool(out.get("hitl")):
            raise ValueError(f"step {step_id!r} cannot clear catalog hitl: true (tighten-only)")
        if ref.hitl is True:
            out["hitl"] = True

    if ref.hitl_max_retries is not None:
        _require_hitl_enabled(step_id, out, field="hitl_max_retries")
        catalog = out.get("hitl_max_retries")
        if catalog is not None and ref.hitl_max_retries > catalog:
            raise ValueError(
                f"step {step_id!r} hitl_max_retries cannot exceed catalog value "
                f"({ref.hitl_max_retries} > {catalog})"
            )
        out["hitl_max_retries"] = ref.hitl_max_retries

    if ref.hitl_retry_guidance is not None:
        _require_hitl_enabled(step_id, out, field="hitl_retry_guidance")
        out["hitl_retry_guidance"] = ref.hitl_retry_guidance


def _tighten_int_field(
    *,
    step_id: str,
    out: dict[str, Any],
    field: str,
    override: int,
    catalog_default: int | None = None,
) -> None:
    catalog = out.get(field, catalog_default if catalog_default is not None else override)
    catalog_int = int(catalog)
    if override > catalog_int:
        raise ValueError(
            f"step {step_id!r} {field} cannot exceed catalog value ({override} > {catalog_int})"
        )
    out[field] = override


def _tighten_optional_token_cap(
    *,
    step_id: str,
    out: dict[str, Any],
    field: str,
    override: int,
    step_kind: str,
    unset_message: str,
) -> None:
    if step_kind != "reason":
        raise ValueError(f"step {step_id!r} {field} override is only valid on reason steps")
    catalog_tokens = out.get(field)
    if catalog_tokens is None:
        raise ValueError(f"step {step_id!r} {unset_message}")
    if override > int(catalog_tokens):
        raise ValueError(
            f"step {step_id!r} {field} cannot exceed catalog value ({override} > {catalog_tokens})"
        )
    out[field] = override


def apply_tighten_overrides(
    *,
    step_id: str,
    capability: dict[str, Any],
    ref: SagaStepRef,
    step_kind: str,
) -> dict[str, Any]:
    """Apply saga-local tighten-only overrides onto a capability dict."""
    out = dict(capability)
    _apply_hitl_overrides(step_id=step_id, out=out, ref=ref)

    if ref.timeout_seconds is not None:
        _tighten_int_field(
            step_id=step_id,
            out=out,
            field="timeout_seconds",
            override=ref.timeout_seconds,
            catalog_default=ref.timeout_seconds,
        )

    if ref.max_turns is not None:
        if step_kind != "reason":
            raise ValueError(f"step {step_id!r} max_turns override is only valid on reason steps")
        _tighten_int_field(
            step_id=step_id,
            out=out,
            field="max_turns",
            override=ref.max_turns,
            catalog_default=ref.max_turns,
        )

    if ref.max_step_tokens is not None:
        _tighten_optional_token_cap(
            step_id=step_id,
            out=out,
            field="max_step_tokens",
            override=ref.max_step_tokens,
            step_kind=step_kind,
            unset_message="cannot set max_step_tokens when the catalog step leaves it unlimited",
        )

    if ref.max_completion_tokens is not None:
        _tighten_optional_token_cap(
            step_id=step_id,
            out=out,
            field="max_completion_tokens",
            override=ref.max_completion_tokens,
            step_kind=step_kind,
            unset_message="cannot set max_completion_tokens when the catalog step leaves it unset",
        )

    return out
