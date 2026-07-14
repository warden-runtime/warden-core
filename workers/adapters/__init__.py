"""Agent adapters implementing common.agent_adapter.AgentAdapterPort."""

from typing import TYPE_CHECKING, Any

from common.agent_adapter import AgentAdapterPort

if TYPE_CHECKING:
    from workers.adapters.langchain import LangChainAdapter

__all__ = [
    "AgentAdapterPort",
    "LangChainAdapter",
]


def __getattr__(name: str) -> Any:
    if name == "LangChainAdapter":
        from workers.adapters.langchain import LangChainAdapter as _LangChainAdapter

        return _LangChainAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
