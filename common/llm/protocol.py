"""
Port and DTOs for chat-model abstraction. No LangChain or provider imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Sequence


class ToolProtocol(Protocol):
    """Minimal tool interface for binding to a chat model. Adapters convert to provider types."""

    name: str

    async def ainvoke(self, args: dict) -> Any:
        """Execute the tool with the given arguments.

        Args:
            args: Tool arguments (dict; schema is tool-specific).

        Returns:
            Tool result (type is tool-specific; may be str or structured).
        """
        ...


class ChatMessage(BaseModel):
    """Single message in a chat turn (system, human, assistant, or tool)."""

    role: Literal["system", "human", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None  # For assistant messages with tool invocations

    model_config = {"extra": "forbid"}


class ToolCall(BaseModel):
    """Single tool invocation request from the model."""

    name: str
    args: dict = Field(default_factory=dict)
    id: str = ""


class ChatResponse(BaseModel):
    """Response from a chat model: content and/or tool calls."""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class ChatModelPort(ABC):
    """Port for chat models. Implementations live in workers (e.g. OpenAI adapter)."""

    def get_underlying_model(self) -> Any:
        """Return the underlying model for use by agent frameworks (e.g. LangGraph), or None if not supported.

        When an adapter (e.g. LangChainAdapter) needs to pass the model to an agent runtime that
        expects a framework-specific type (e.g. LangChain BaseChatModel), it calls this instead of
        relying on implementation details. Implementations that support agent frameworks override
        and return the appropriate instance; others rely on the default None.
        """
        return None

    @abstractmethod
    def bind_tools(self, tools: Sequence[ToolProtocol]) -> Self:
        """Return a new port instance that will use these tools when ainvoke is called.

        Args:
            tools: Sequence of ToolProtocol instances to bind.

        Returns:
            A new ChatModelPort instance (or Self) with tools bound.
        """
        ...

    @abstractmethod
    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse:
        """Run the model on the given messages; return content and/or tool calls.

        Args:
            messages: Ordered sequence of chat messages (system, human, assistant, tool).

        Returns:
            ChatResponse with content and/or tool_calls.
        """
        ...

    def bind_json_schema(self, schema: dict[str, Any]) -> Self:
        """Return a port that constrains ainvoke to JSON matching schema.

        Default: not supported; provider adapters override or wrap with SchemaBoundChatModel.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support bind_json_schema; use a provider adapter."
        )
