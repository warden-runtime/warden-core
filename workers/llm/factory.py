"""
Factory for building chat model port implementations by provider.
"""

import logging
import os

from common.llm import ChatModelPort
from workers.llm.mock import MockChatAdapter
from workers.llm.openai import OpenAIChatAdapter
from workers.llm.retrying import wrap_llm_with_retry

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"
_LOCAL_API_KEY_PLACEHOLDER = "local-token-not-required"


def build_llm(
    provider: str,
    model_name: str,
    api_key: str,
    temperature: float = 0.0,
) -> ChatModelPort:
    """
    Build a chat model port implementation for the given provider.

    Args:
        provider: Provider identifier (e.g. "openai", "local", "anthropic").
        model_name: Model name (e.g. gpt-4o).
        api_key: Provider API key.
        temperature: Sampling temperature.

    Returns:
        ChatModelPort implementation (e.g. OpenAIChatAdapter).

    Raises:
        ValueError: If provider is not supported (e.g. anthropic not yet implemented).
    """
    normalized = (provider or "").strip().lower()
    if normalized == "openai":
        return wrap_llm_with_retry(
            OpenAIChatAdapter(
                model_name=model_name,
                api_key=api_key,
                temperature=temperature,
            )
        )
    if normalized == "local":
        base_url = os.environ.get("WARDEN_LOCAL_LLM_BASE_URL", _DEFAULT_LOCAL_BASE_URL).strip()
        resolved_key = (api_key or "").strip() or _LOCAL_API_KEY_PLACEHOLDER
        logger.info(
            "Initializing local OpenAI-compatible LLM: model=%s base_url=%s",
            model_name,
            base_url,
        )
        return wrap_llm_with_retry(
            OpenAIChatAdapter(
                model_name=model_name,
                api_key=resolved_key,
                temperature=temperature,
                base_url=base_url,
            )
        )
    if normalized == "mock":
        logger.info("Initializing mock LLM for demo: model=%s", model_name)
        return MockChatAdapter()
    if normalized == "anthropic":
        logger.error(
            "Provider 'anthropic' is not implemented; add langchain-anthropic and an adapter."
        )
        raise ValueError(
            "Provider 'anthropic' is not implemented. "
            "Install langchain-anthropic and add workers.llm.anthropic adapter, or use provider='openai'."
        )
    logger.error("Unknown LLM provider: %s", provider)
    raise ValueError(f"Unknown LLM provider: {provider!r}. Supported: openai, local, mock.")
