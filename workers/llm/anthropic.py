"""
Anthropic chat model adapter implementing the common ChatModelPort.
"""

import logging
from collections.abc import Sequence
from typing import Any, cast

from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolProtocol
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from workers.llm.message_content import aimessage_to_chat_response, chat_message_to_langchain
from workers.llm.structured import SchemaBoundChatModel

logger = logging.getLogger(__name__)


class AnthropicChatAdapter(ChatModelPort):
    """
    Chat model port implementation using Anthropic Claude via LangChain.

    Holds a ChatAnthropic instance; bind_tools returns a new adapter wrapping
    the bound model. ainvoke accepts our ChatMessage list and returns ChatResponse.
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        temperature: float = 0.0,
        *,
        _llm: Any = None,
    ) -> None:
        """
        Args:
            model_name: Anthropic model identifier (e.g. claude-3-5-sonnet-20241022).
            api_key: Anthropic API key.
            temperature: Sampling temperature.
            _llm: Optional pre-bound LLM (used by bind_tools).
        """
        self._llm: Any
        if _llm is not None:
            self._llm = _llm
        else:
            llm_kwargs: dict[str, Any] = {
                "api_key": api_key,
                "model": model_name,
                "temperature": temperature,
                # Retries live in wrap_llm_with_retry; avoid stacking provider retries.
                "max_retries": 0,
            }
            self._llm = ChatAnthropic(**llm_kwargs)

    def get_underlying_model(self) -> Any:
        """Return the underlying LangChain chat model (legacy agent integration)."""
        return self._llm

    def bind_tools(self, tools: Sequence[ToolProtocol]) -> "AnthropicChatAdapter":
        """
        Return a new adapter that uses the given tools when ainvoke is called.

        Args:
            tools: Tools the model may call (e.g. LangChain StructuredTool list).

        Returns:
            New AnthropicChatAdapter wrapping the bound model.
        """
        bound = self._llm.bind_tools(cast("Any", list(tools)))
        llm_temperature = self._llm.temperature
        return AnthropicChatAdapter(
            model_name=self._llm.model or "",
            api_key=getattr(self._llm, "api_key", "") or "",
            temperature=0.0 if llm_temperature is None else float(llm_temperature),
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
            lc_messages = [chat_message_to_langchain(m) for m in messages]
            aimessage = await self._llm.ainvoke(lc_messages)
            if not isinstance(aimessage, AIMessage):
                logger.error("Unexpected response type: %s", type(aimessage), exc_info=False)
                raise TypeError(f"Expected AIMessage, got {type(aimessage)}")
            return aimessage_to_chat_response(aimessage)
        except Exception:
            logger.exception("Anthropic adapter ainvoke failed")
            raise
