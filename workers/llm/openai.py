"""
OpenAI chat model adapter implementing the common ChatModelPort.
"""

import logging
from collections.abc import Sequence
from typing import Any, cast

from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolCall, ToolProtocol
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from workers.llm.structured import SchemaBoundChatModel

logger = logging.getLogger(__name__)


def _chat_message_to_langchain(msg: ChatMessage) -> BaseMessage:
    """Convert a ChatMessage to the corresponding LangChain message type."""
    if msg.role == "system":
        return SystemMessage(content=msg.content)
    if msg.role == "human":
        return HumanMessage(content=msg.content)
    if msg.role == "assistant":
        lc_tool_calls = None
        if msg.tool_calls:
            lc_tool_calls = [
                {"name": tc.name, "args": tc.args, "id": tc.id} for tc in msg.tool_calls
            ]
        return AIMessage(content=msg.content, tool_calls=lc_tool_calls or [])
    if msg.role == "tool":
        return ToolMessage(
            content=msg.content,
            tool_call_id=msg.tool_call_id or "",
            name=msg.name or "",
        )
    raise ValueError(f"Unknown ChatMessage role: {msg.role!r}")


def _aimessage_to_chat_response(aimessage: AIMessage) -> ChatResponse:
    """Convert a LangChain AIMessage to ChatResponse."""
    content = aimessage.content if isinstance(aimessage.content, str) else None
    tool_calls: list[ToolCall] = []
    for tc in getattr(aimessage, "tool_calls", []) or []:
        if hasattr(tc, "get"):
            tool_calls.append(
                ToolCall(
                    name=tc.get("name", ""),
                    args=tc.get("args") or {},
                    id=tc.get("id") or "",
                )
            )
        else:
            tool_calls.append(
                ToolCall(
                    name=getattr(tc, "name", ""),
                    args=getattr(tc, "args", None) or {},
                    id=getattr(tc, "id", "") or "",
                )
            )
    return ChatResponse(content=content, tool_calls=tool_calls)


class OpenAIChatAdapter(ChatModelPort):
    """
    Chat model port implementation using OpenAI via LangChain.

    Holds a ChatOpenAI instance; bind_tools returns a new adapter wrapping
    the bound model. ainvoke accepts our ChatMessage list and returns ChatResponse.
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        temperature: float = 0.0,
        *,
        base_url: str | None = None,
        _llm: Any = None,
    ) -> None:
        """
        Args:
            model_name: OpenAI model identifier (e.g. gpt-4o).
            api_key: OpenAI API key.
            temperature: Sampling temperature.
            base_url: Optional OpenAI-compatible API base (e.g. local Ollama/vLLM).
            _llm: Optional pre-bound LLM (used by bind_tools).
        """
        self._base_url = base_url
        self._llm: Any
        if _llm is not None:
            self._llm = _llm
        else:
            llm_kwargs: dict[str, Any] = {
                "api_key": api_key,
                "model": model_name,
                "temperature": temperature,
                "max_retries": 0,
            }
            if base_url:
                llm_kwargs["base_url"] = base_url
            self._llm = ChatOpenAI(**llm_kwargs)

    def get_underlying_model(self) -> Any:
        """Return the underlying LangChain chat model (legacy agent integration)."""
        return self._llm

    def bind_tools(self, tools: Sequence[ToolProtocol]) -> "OpenAIChatAdapter":
        """
        Return a new adapter that uses the given tools when ainvoke is called.

        Args:
            tools: Tools the model may call (e.g. LangChain StructuredTool list).

        Returns:
            New OpenAIChatAdapter wrapping the bound model.
        """
        bound = self._llm.bind_tools(cast("Any", list(tools)))
        llm_temperature = self._llm.temperature
        return OpenAIChatAdapter(
            model_name=self._llm.model_name or "",
            api_key=getattr(self._llm, "api_key", "") or "",
            temperature=0.0 if llm_temperature is None else float(llm_temperature),
            base_url=self._base_url,
            _llm=bound,
        )

    def bind_json_schema(self, schema: dict[str, Any]) -> SchemaBoundChatModel:
        return SchemaBoundChatModel(self, schema)

    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse:
        """
        Run the model on the given messages; return content and/or tool calls.

        Args:
            messages: Ordered list of chat messages (system, human, assistant, tool).

        Returns:
            ChatResponse with content and/or tool_calls.

        Raises:
            Exception: Re-raised after logging on LLM or conversion failure.
        """
        try:
            lc_messages = [_chat_message_to_langchain(m) for m in messages]
            aimessage = await self._llm.ainvoke(lc_messages)
            if not isinstance(aimessage, AIMessage):
                logger.error("Unexpected response type: %s", type(aimessage), exc_info=False)
                raise TypeError(f"Expected AIMessage, got {type(aimessage)}")
            return _aimessage_to_chat_response(aimessage)
        except Exception:
            logger.exception("OpenAI adapter ainvoke failed")
            raise
