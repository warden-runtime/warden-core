"""
Resolves WorkerDefinition (+ ProviderSecret) to an AgentAdapterPort implementation.
"""

from types import SimpleNamespace
from typing import Any

from common.agent_adapter import AgentAdapterPort
from common.models import ProviderSecret, WorkerDefinition
from workers.adapters.langchain import LangChainAdapter

ProviderCredential = ProviderSecret | SimpleNamespace


def resolve_adapter(
    worker_definition: WorkerDefinition,
    secret: ProviderCredential,
    context: dict[str, Any] | None = None,
) -> AgentAdapterPort:
    """Return an AgentAdapterPort implementation for the given worker and secret.

    Args:
        worker_definition: Worker config (adapter name, model, prompts, etc.).
        secret: Provider API key/secret for the worker's model_provider.
        context: Optional context passed to the adapter (e.g. headers).

    Returns:
        AgentAdapterPort instance (e.g. LangChainAdapter).

    Raises:
        ValueError: If adapter name is unknown or not implemented (e.g. "adk").
    """
    adapter_name = getattr(worker_definition, "adapter", "langchain").strip().lower() or "langchain"
    if adapter_name == "langchain":
        return LangChainAdapter(
            worker_definition=worker_definition,
            secret=secret,
            context=context,
        )
    if adapter_name == "adk":
        raise ValueError("Adapter 'adk' is not implemented. Use adapter='langchain'.")
    raise ValueError(f"Unknown adapter {adapter_name!r}. Supported: langchain.")
