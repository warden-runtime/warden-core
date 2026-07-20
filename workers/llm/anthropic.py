"""
Anthropic chat model adapter implementing the common ChatModelPort.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolProtocol
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.tools import BaseTool
from workers.llm.message_content import aimessage_to_chat_response, chat_message_to_langchain
from workers.llm.structured import SchemaBoundChatModel

logger = logging.getLogger(__name__)

# Anthropic prompt caching: 5m ephemeral (default TTL). Always-on for this adapter.
_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral", "ttl": "5m"}


def _tag_last_tool_for_cache(tools: Sequence[Any]) -> list[Any]:
    """Attach cache_control to the last tool so the whole tools block is cacheable.

    Supports LangChain ``BaseTool`` (via ``extras``) and raw Anthropic/OpenAI-style
    tool dicts (top-level ``cache_control``). Other shapes are left unchanged.
    """
    if not tools:
        return list(tools)

    tagged = list(tools)
    last = tagged[-1]

    if isinstance(last, BaseTool):
        new_extras = {**(last.extras or {}), "cache_control": dict(_CACHE_CONTROL)}
        tagged[-1] = last.model_copy(update={"extras": new_extras})
        return tagged

    if isinstance(last, Mapping):
        tagged[-1] = {**dict(last), "cache_control": dict(_CACHE_CONTROL)}
        return tagged

    return tagged


def _tag_system_message_for_cache(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Put cache_control on the first non-empty system message content block."""
    out: list[BaseMessage] = []
    tagged = False
    for msg in messages:
        if tagged or not isinstance(msg, SystemMessage):
            out.append(msg)
            continue
        content = msg.content
        if isinstance(content, str) and content:
            out.append(
                SystemMessage(
                    content=[
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": dict(_CACHE_CONTROL),
                        }
                    ]
                )
            )
            tagged = True
            continue
        if isinstance(content, list) and content:
            new_content = list(content)
            last = new_content[-1]
            if isinstance(last, dict):
                new_content[-1] = {**last, "cache_control": dict(_CACHE_CONTROL)}
            elif isinstance(last, str) and last:
                new_content[-1] = {
                    "type": "text",
                    "text": last,
                    "cache_control": dict(_CACHE_CONTROL),
                }
            else:
                out.append(msg)
                continue
            out.append(SystemMessage(content=new_content))
            tagged = True
            continue
        out.append(msg)
    return out


class AnthropicChatAdapter(ChatModelPort):
    """
    Chat model port implementation using Anthropic Claude via LangChain.

    Holds a ChatAnthropic instance; bind_tools returns a new adapter wrapping
    the bound model. ainvoke accepts our ChatMessage list and returns ChatResponse.

    Prompt caching is always enabled (ephemeral, 5m TTL): the last bound tool and
    the system message get cache breakpoints, and each ainvoke passes top-level
    ``cache_control`` so Anthropic can cache the growing message prefix across
    ReAct turns.
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

        Tags the last ``BaseTool`` or tool dict with ``cache_control`` so Anthropic
        can cache the contiguous tools block across turns.

        Args:
            tools: Tools the model may call (e.g. LangChain StructuredTool list).

        Returns:
            New AnthropicChatAdapter wrapping the bound model.
        """
        tagged_tools = _tag_last_tool_for_cache(cast("Sequence[Any]", tools))
        bound = self._llm.bind_tools(cast("Any", tagged_tools))
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
            lc_messages = _tag_system_message_for_cache(
                [chat_message_to_langchain(m) for m in messages]
            )
            aimessage = await self._llm.ainvoke(
                lc_messages,
                cache_control=dict(_CACHE_CONTROL),
            )
            if not isinstance(aimessage, AIMessage):
                logger.error("Unexpected response type: %s", type(aimessage), exc_info=False)
                raise TypeError(f"Expected AIMessage, got {type(aimessage)}")
            return aimessage_to_chat_response(aimessage)
        except Exception:
            logger.exception("Anthropic adapter ainvoke failed")
            raise
