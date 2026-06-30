"""Unit tests for common.plugins.tortoise_modules."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from common.plugins import register_policy_hooks, reset_registry
from common.plugins.tortoise_modules import model_modules_for_registry


@dataclass
class _PolicyWithModules:
    modules: list[str] = field(default_factory=list)

    def get_required_modules(self) -> list[str]:
        return list(self.modules)


@pytest.fixture(autouse=True)
def _isolated_registry():
    reset_registry()
    yield
    reset_registry()


def test_model_modules_for_registry_includes_core_models():
    assert model_modules_for_registry() == ["common.models"]


def test_model_modules_for_registry_extends_with_policy_modules():
    register_policy_hooks(_PolicyWithModules(modules=["enterprise.models"]))
    assert model_modules_for_registry() == ["common.models", "enterprise.models"]
