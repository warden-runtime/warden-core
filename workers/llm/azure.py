"""
Azure OpenAI / Microsoft Foundry chat model adapter (OpenAI-compatible v1 API).
"""

import logging
import os
import re
from collections.abc import Sequence
from typing import Any, cast

from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolProtocol
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from workers.llm.message_content import aimessage_to_chat_response, chat_message_to_langchain
from workers.llm.structured import SchemaBoundChatModel

logger = logging.getLogger(__name__)

_ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
_USE_RESPONSES_ENV = "WARDEN_AZURE_USE_RESPONSES_API"
_CACHE_RETENTION_ENV = "WARDEN_AZURE_PROMPT_CACHE_RETENTION"
# Foundry project portal URLs look like:
#   https://<resource>.services.ai.azure.com/api/projects/<project>
# Inference uses the sibling OpenAI-compatible path /openai/v1/ on the same host.
_PROJECT_PATH_RE = re.compile(r"/api/projects/[^/]+/?$", re.IGNORECASE)


def azure_v1_base_url(endpoint: str) -> str:
    """Build Foundry / Azure OpenAI-compatible v1 base URL from a resource endpoint.

    Accepts any of:
    - ``https://<resource>.openai.azure.com``
    - ``https://<resource>.services.ai.azure.com``
    - ``https://<resource>.services.ai.azure.com/openai/v1``
    - ``https://<resource>.services.ai.azure.com/api/projects/<project>`` (normalized)
    """
    root = (endpoint or "").strip().rstrip("/")
    if not root:
        raise ValueError(
            f"{_ENDPOINT_ENV} is required for provider: azure "
            "(e.g. https://YOUR-RESOURCE.services.ai.azure.com or "
            "https://YOUR-RESOURCE.openai.azure.com/)."
        )
    root = _PROJECT_PATH_RE.sub("", root).rstrip("/")
    if root.endswith("/openai/v1"):
        return root + "/"
    return f"{root}/openai/v1/"


def resolve_azure_endpoint() -> str:
    """Return AZURE_OPENAI_ENDPOINT or raise if unset/empty."""
    raw = os.environ.get(_ENDPOINT_ENV)
    endpoint = (raw or "").strip()
    if not endpoint:
        raise ValueError(
            f"{_ENDPOINT_ENV} is required for provider: azure "
            "(e.g. https://YOUR-RESOURCE.services.ai.azure.com or "
            "https://YOUR-RESOURCE.openai.azure.com/)."
        )
    return endpoint


def _env_flag_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _prompt_cache_retention_from_env() -> str | None:
    """Optional Azure prompt_cache_retention (``in_memory`` or ``24h``)."""
    raw = (os.environ.get(_CACHE_RETENTION_ENV) or "").strip()
    if not raw:
        return None
    if raw not in {"in_memory", "24h"}:
        logger.warning(
            "Ignoring invalid %s=%r (expected in_memory or 24h)",
            _CACHE_RETENTION_ENV,
            raw,
        )
        return None
    return raw


def _build_chat_openai(
    *,
    model_name: str,
    api_key: str,
    temperature: float,
    base_url: str,
) -> Any:
    """Build ChatOpenAI with cache-friendly defaults for Foundry / Azure OpenAI.

    Chat Completions is the default: Azure prompt caching is automatic on identical
    prefixes and is well-proven on chat.completions. The Responses API is opt-in
    via ``WARDEN_AZURE_USE_RESPONSES_API`` (portal samples often use it; some Azure
    deployments report weak or zero ``cached_tokens`` on Responses).

    Azure does not support Anthropic-style ``cache_control`` / breakpoints; keep
    system + tools stable and put variable turns last (ReAct already does this).
    Optional ``WARDEN_AZURE_PROMPT_CACHE_RETENTION=in_memory|24h`` is passed through.
    """
    use_responses = _env_flag_true(_USE_RESPONSES_ENV)
    retention = _prompt_cache_retention_from_env()
    llm_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "model": model_name,
        "temperature": temperature,
        "max_retries": 0,
        "base_url": base_url,
        "use_responses_api": use_responses,
    }
    if retention is not None:
        # Top-level request field on Chat Completions / Responses (Azure Foundry).
        llm_kwargs["model_kwargs"] = {"prompt_cache_retention": retention}
    logger.info(
        "Azure LLM transport: responses_api=%s prompt_cache_retention=%s",
        use_responses,
        retention,
    )
    return ChatOpenAI(**llm_kwargs)


class AzureChatAdapter(ChatModelPort):
    """
    Chat model port for Azure OpenAI / Foundry via ChatOpenAI + /openai/v1/.

    ``model_name`` is the Foundry **deployment name**. Endpoint comes from
    ``AZURE_OPENAI_ENDPOINT`` (or an explicit ``azure_endpoint`` argument).
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        temperature: float = 0.0,
        *,
        azure_endpoint: str | None = None,
        _llm: Any = None,
        _base_url: str | None = None,
    ) -> None:
        """
        Args:
            model_name: Foundry deployment name (e.g. gpt-5.4-mini-1).
            api_key: Azure / Foundry API key (AZURE_OPENAI_API_KEY).
            temperature: Sampling temperature.
            azure_endpoint: Resource endpoint; defaults to AZURE_OPENAI_ENDPOINT.
            _llm: Optional pre-bound LLM (used by bind_tools).
            _base_url: Cached v1 base URL when wrapping a bound model.
        """
        if _llm is not None:
            self._base_url = _base_url or ""
            self._llm = _llm
            return

        endpoint = azure_endpoint if azure_endpoint is not None else resolve_azure_endpoint()
        self._base_url = azure_v1_base_url(endpoint)
        logger.info(
            "Initializing Azure OpenAI LLM: deployment=%s base_url=%s",
            model_name,
            self._base_url,
        )
        self._llm = _build_chat_openai(
            model_name=model_name,
            api_key=api_key,
            temperature=temperature,
            base_url=self._base_url,
        )

    def get_underlying_model(self) -> Any:
        """Return the underlying LangChain chat model."""
        return self._llm

    def bind_tools(self, tools: Sequence[ToolProtocol]) -> "AzureChatAdapter":
        """Return a new adapter that uses the given tools when ainvoke is called."""
        bound = self._llm.bind_tools(cast("Any", list(tools)))
        llm_temperature = self._llm.temperature
        return AzureChatAdapter(
            model_name=self._llm.model_name or "",
            api_key=getattr(self._llm, "api_key", "") or "",
            temperature=0.0 if llm_temperature is None else float(llm_temperature),
            _llm=bound,
            _base_url=self._base_url,
        )

    def bind_json_schema(self, schema: dict[str, Any]) -> SchemaBoundChatModel:
        return SchemaBoundChatModel(self, schema)

    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse:
        """Run the model on the given messages; return content and/or tool calls."""
        try:
            lc_messages = [chat_message_to_langchain(m) for m in messages]
            aimessage = await self._llm.ainvoke(lc_messages)
            if not isinstance(aimessage, AIMessage):
                logger.error("Unexpected response type: %s", type(aimessage), exc_info=False)
                raise TypeError(f"Expected AIMessage, got {type(aimessage)}")
            return aimessage_to_chat_response(aimessage)
        except Exception:
            logger.exception("Azure adapter ainvoke failed")
            raise
