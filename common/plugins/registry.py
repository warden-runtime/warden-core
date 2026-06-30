"""Process-wide plugin registry (lazy singleton, fail-open NoOp defaults)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.plugins.noop import default_registry_hooks

if TYPE_CHECKING:
    from common.plugins.protocols import (
        AdapterHooks,
        CliExtensionRegistry,
        EngineLifecycleHooks,
        HttpExtensionRegistry,
        MessagingFactory,
        PolicyGateHooks,
        ToolLifecycleHooks,
        WorkerLifecycleHooks,
    )


@dataclass
class PluginRegistry:
    engine: EngineLifecycleHooks
    policy: PolicyGateHooks
    worker: WorkerLifecycleHooks
    tools: ToolLifecycleHooks
    adapter: AdapterHooks
    http: HttpExtensionRegistry
    cli: CliExtensionRegistry
    messaging: MessagingFactory


_registry: PluginRegistry | None = None


def _build_default_registry() -> PluginRegistry:
    defaults = default_registry_hooks()
    return PluginRegistry(**defaults)


def get_registry() -> PluginRegistry:
    """Return the process-wide plugin registry (lazy-init with NoOp defaults)."""
    global _registry
    if _registry is None:
        _registry = _build_default_registry()
    return _registry


def reset_registry() -> None:
    """Restore default NoOp registry (tests only)."""
    global _registry
    _registry = _build_default_registry()


def register_engine_hooks(hooks: EngineLifecycleHooks) -> None:
    get_registry().engine = hooks


def register_policy_hooks(hooks: PolicyGateHooks) -> None:
    get_registry().policy = hooks


def register_worker_hooks(hooks: WorkerLifecycleHooks) -> None:
    get_registry().worker = hooks


def register_tool_hooks(hooks: ToolLifecycleHooks) -> None:
    get_registry().tools = hooks


def register_adapter_hooks(hooks: AdapterHooks) -> None:
    get_registry().adapter = hooks


def register_http_extensions(registry: HttpExtensionRegistry) -> None:
    get_registry().http = registry


def register_cli_extensions(registry: CliExtensionRegistry) -> None:
    get_registry().cli = registry


def register_messaging_factory(factory: MessagingFactory) -> None:
    get_registry().messaging = factory
