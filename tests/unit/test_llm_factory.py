"""Unit tests for the LLM factory."""

import pytest
from common.config import get_settings
from common.llm import ChatModelPort
from workers.llm import build_llm
from workers.llm.mock import MockChatAdapter
from workers.llm.openai import OpenAIChatAdapter
from workers.llm.retrying import RetryingChatModelPort


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _inner_adapter(llm: ChatModelPort) -> OpenAIChatAdapter:
    if isinstance(llm, RetryingChatModelPort):
        inner = llm._inner
        assert isinstance(inner, OpenAIChatAdapter)
        return inner
    assert isinstance(llm, OpenAIChatAdapter)
    return llm


def test_build_llm_openai_returns_retrying_wrapper_by_default():
    """build_llm('openai', ...) wraps OpenAIChatAdapter in RetryingChatModelPort."""
    llm = build_llm(
        provider="openai",
        model_name="gpt-4o",
        api_key="sk-fake",
    )
    assert isinstance(llm, RetryingChatModelPort)
    assert isinstance(llm, ChatModelPort)
    assert isinstance(_inner_adapter(llm), OpenAIChatAdapter)


def test_build_llm_openai_normalizes_provider():
    """build_llm normalizes provider to lowercase."""
    llm = build_llm(provider="OPENAI", model_name="gpt-4o", api_key="sk-fake")
    assert isinstance(llm, RetryingChatModelPort)
    assert isinstance(_inner_adapter(llm), OpenAIChatAdapter)


def test_build_llm_local_uses_openai_adapter_with_base_url(monkeypatch):
    """build_llm('local', ...) returns OpenAIChatAdapter configured for a local base URL."""
    monkeypatch.setenv("WARDEN_LOCAL_LLM_BASE_URL", "http://ollama.test/v1")
    llm = build_llm(provider="local", model_name="llama3", api_key="")
    adapter = _inner_adapter(llm)
    assert adapter._base_url == "http://ollama.test/v1"
    assert adapter._llm.openai_api_base == "http://ollama.test/v1"


def test_build_llm_local_default_base_url_when_env_unset(monkeypatch):
    """local provider falls back to default Ollama-style base URL."""
    monkeypatch.delenv("WARDEN_LOCAL_LLM_BASE_URL", raising=False)
    llm = build_llm(provider="local", model_name="llama3", api_key="")
    assert _inner_adapter(llm)._base_url == "http://localhost:11434/v1"


def test_build_llm_retry_disabled_returns_bare_adapter(monkeypatch):
    """When WARDEN_LLM_RETRY_ENABLED=false, build_llm returns OpenAIChatAdapter only."""
    monkeypatch.setenv("WARDEN_LLM_RETRY_ENABLED", "false")
    llm = build_llm(provider="openai", model_name="gpt-4o", api_key="sk-fake")
    assert isinstance(llm, OpenAIChatAdapter)
    assert not isinstance(llm, RetryingChatModelPort)


def test_build_llm_mock_returns_bare_adapter_without_retry():
    llm = build_llm(provider="mock", model_name="demo-greet", api_key="")
    assert isinstance(llm, MockChatAdapter)
    assert not isinstance(llm, RetryingChatModelPort)


def test_build_llm_unknown_provider_raises():
    """build_llm raises ValueError for unknown provider."""
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_llm(provider="unknown-provider", model_name="x", api_key="y")


def test_build_llm_anthropic_raises_clear_message():
    """build_llm raises ValueError for anthropic with message to implement."""
    with pytest.raises(ValueError, match="anthropic") as exc_info:
        build_llm(provider="anthropic", model_name="claude-3", api_key="sk-fake")
    assert (
        "not implemented" in str(exc_info.value).lower()
        or "anthropic" in str(exc_info.value).lower()
    )
