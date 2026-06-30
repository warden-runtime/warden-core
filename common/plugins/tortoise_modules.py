"""Resolve Tortoise model modules from the active plugin registry."""

from __future__ import annotations

from common.plugins.registry import get_registry


def model_modules_for_registry() -> list[str]:
    """Return Tortoise model module paths for the current registry."""
    modules = ["common.models"]
    modules.extend(get_registry().policy.get_required_modules())
    return modules
