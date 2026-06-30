"""Open Core plugin registry and hook protocols."""

from common.plugins.context import (
    ExecutionScope,
    db_conn_from_injection,
    execution_scope_from_injection,
)
from common.plugins.loader import load_plugins_from_env
from common.plugins.messaging_wire import wire_messaging_from_registry
from common.plugins.noop import (
    DefaultMessagingFactory,
    NoOpAdapterHooks,
    NoOpCliExtensionRegistry,
    NoOpEngineLifecycleHooks,
    NoOpHttpExtensionRegistry,
    NoOpPolicyGateHooks,
    NoOpToolLifecycleHooks,
    NoOpWorkerLifecycleHooks,
)
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
from common.plugins.registry import (
    PluginRegistry,
    get_registry,
    register_adapter_hooks,
    register_cli_extensions,
    register_engine_hooks,
    register_http_extensions,
    register_messaging_factory,
    register_policy_hooks,
    register_tool_hooks,
    register_worker_hooks,
    reset_registry,
)
from common.plugins.tortoise_modules import model_modules_for_registry

__all__ = [
    "AdapterHooks",
    "CliExtensionRegistry",
    "DefaultMessagingFactory",
    "EngineLifecycleHooks",
    "ExecutionScope",
    "HttpExtensionRegistry",
    "MessagingFactory",
    "NoOpAdapterHooks",
    "NoOpCliExtensionRegistry",
    "NoOpEngineLifecycleHooks",
    "NoOpHttpExtensionRegistry",
    "NoOpPolicyGateHooks",
    "NoOpToolLifecycleHooks",
    "NoOpWorkerLifecycleHooks",
    "PluginRegistry",
    "PolicyGateHooks",
    "ToolLifecycleHooks",
    "WorkerLifecycleHooks",
    "db_conn_from_injection",
    "execution_scope_from_injection",
    "get_registry",
    "load_plugins_from_env",
    "model_modules_for_registry",
    "register_adapter_hooks",
    "register_cli_extensions",
    "register_engine_hooks",
    "register_http_extensions",
    "register_messaging_factory",
    "register_policy_hooks",
    "register_tool_hooks",
    "register_worker_hooks",
    "reset_registry",
    "wire_messaging_from_registry",
]
