"""Unit tests for the LLM factory."""

import pytest
from common.config import get_settings
from common.llm import ChatModelPort
from workers.llm import build_llm
from workers.llm.anthropic import AnthropicChatAdapter
from workers.llm.azure import AzureChatAdapter, azure_v1_base_url
from workers.llm.mock import MockChatAdapter
from workers.llm.openai import OpenAIChatAdapter
from workers.llm.retrying import RetryingChatModelPort


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _inner_openai(llm: ChatModelPort) -> OpenAIChatAdapter:
    if isinstance(llm, RetryingChatModelPort):
        inner = llm._inner
        assert isinstance(inner, OpenAIChatAdapter)
        return inner
    assert isinstance(llm, OpenAIChatAdapter)
    return llm


def _inner_anthropic(llm: ChatModelPort) -> AnthropicChatAdapter:
    if isinstance(llm, RetryingChatModelPort):
        inner = llm._inner
        assert isinstance(inner, AnthropicChatAdapter)
        return inner
    assert isinstance(llm, AnthropicChatAdapter)
    return llm


def _inner_azure(llm: ChatModelPort) -> AzureChatAdapter:
    if isinstance(llm, RetryingChatModelPort):
        inner = llm._inner
        assert isinstance(inner, AzureChatAdapter)
        return inner
    assert isinstance(llm, AzureChatAdapter)
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
    assert isinstance(_inner_openai(llm), OpenAIChatAdapter)


def test_build_llm_openai_normalizes_provider():
    """build_llm normalizes provider to lowercase."""
    llm = build_llm(provider="OPENAI", model_name="gpt-4o", api_key="sk-fake")
    assert isinstance(llm, RetryingChatModelPort)
    assert isinstance(_inner_openai(llm), OpenAIChatAdapter)


def test_build_llm_local_uses_openai_adapter_with_base_url(monkeypatch):
    """build_llm('local', ...) returns OpenAIChatAdapter configured for a local base URL."""
    monkeypatch.setenv("WARDEN_LOCAL_LLM_BASE_URL", "http://ollama.test/v1")
    llm = build_llm(provider="local", model_name="llama3", api_key="")
    adapter = _inner_openai(llm)
    assert adapter._base_url == "http://ollama.test/v1"
    assert adapter._llm.openai_api_base == "http://ollama.test/v1"


def test_build_llm_local_default_base_url_when_env_unset(monkeypatch):
    """local provider falls back to default Ollama-style base URL."""
    monkeypatch.delenv("WARDEN_LOCAL_LLM_BASE_URL", raising=False)
    llm = build_llm(provider="local", model_name="llama3", api_key="")
    assert _inner_openai(llm)._base_url == "http://localhost:11434/v1"


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


def test_build_llm_anthropic_returns_retrying_wrapper_by_default():
    """build_llm('anthropic', ...) wraps AnthropicChatAdapter in RetryingChatModelPort."""
    llm = build_llm(
        provider="anthropic",
        model_name="claude-3-5-sonnet-20241022",
        api_key="sk-ant-fake",
    )
    assert isinstance(llm, RetryingChatModelPort)
    assert isinstance(llm, ChatModelPort)
    assert isinstance(_inner_anthropic(llm), AnthropicChatAdapter)


def test_build_llm_anthropic_normalizes_provider():
    """build_llm normalizes anthropic provider to lowercase."""
    llm = build_llm(
        provider="ANTHROPIC",
        model_name="claude-3-5-sonnet-20241022",
        api_key="sk-ant-fake",
    )
    assert isinstance(llm, RetryingChatModelPort)
    assert isinstance(_inner_anthropic(llm), AnthropicChatAdapter)


def test_build_llm_anthropic_retry_disabled_returns_bare_adapter(monkeypatch):
    """When WARDEN_LLM_RETRY_ENABLED=false, build_llm returns AnthropicChatAdapter only."""
    monkeypatch.setenv("WARDEN_LLM_RETRY_ENABLED", "false")
    llm = build_llm(
        provider="anthropic",
        model_name="claude-3-5-sonnet-20241022",
        api_key="sk-ant-fake",
    )
    assert isinstance(llm, AnthropicChatAdapter)


def test_build_llm_openai_passes_max_tokens(monkeypatch):
    monkeypatch.setenv("WARDEN_LLM_RETRY_ENABLED", "false")
    llm = build_llm(
        provider="openai",
        model_name="gpt-4o",
        api_key="sk-fake",
        max_tokens=4096,
    )
    assert isinstance(llm, OpenAIChatAdapter)
    assert llm._max_tokens == 4096
    assert llm._llm.max_tokens == 4096


def test_build_llm_openai_bind_tools_preserves_max_tokens(monkeypatch):
    monkeypatch.setenv("WARDEN_LLM_RETRY_ENABLED", "false")
    llm = build_llm(
        provider="openai",
        model_name="gpt-4o",
        api_key="sk-fake",
        max_tokens=2048,
    )
    bound = llm.bind_tools([])
    assert isinstance(bound, OpenAIChatAdapter)
    assert bound._max_tokens == 2048


def test_build_llm_anthropic_passes_max_tokens(monkeypatch):
    monkeypatch.setenv("WARDEN_LLM_RETRY_ENABLED", "false")
    llm = build_llm(
        provider="anthropic",
        model_name="claude-3-5-sonnet-20241022",
        api_key="sk-ant-fake",
        max_tokens=8192,
    )
    assert isinstance(llm, AnthropicChatAdapter)
    assert llm._max_tokens == 8192
    assert llm._llm.max_tokens == 8192
    assert not isinstance(llm, RetryingChatModelPort)


def test_azure_v1_base_url_appends_openai_v1():
    assert (
        azure_v1_base_url("https://my-resource.openai.azure.com/")
        == "https://my-resource.openai.azure.com/openai/v1/"
    )
    assert (
        azure_v1_base_url("https://my-resource.openai.azure.com")
        == "https://my-resource.openai.azure.com/openai/v1/"
    )
    assert (
        azure_v1_base_url("https://my-resource.openai.azure.com/openai/v1")
        == "https://my-resource.openai.azure.com/openai/v1/"
    )


def test_azure_v1_base_url_foundry_services_ai():
    assert (
        azure_v1_base_url("https://my-project-resource.services.ai.azure.com")
        == "https://my-project-resource.services.ai.azure.com/openai/v1/"
    )
    assert (
        azure_v1_base_url("https://my-project-resource.services.ai.azure.com/openai/v1")
        == "https://my-project-resource.services.ai.azure.com/openai/v1/"
    )
    assert (
        azure_v1_base_url(
            "https://my-project-resource.services.ai.azure.com/api/projects/my-project"
        )
        == "https://my-project-resource.services.ai.azure.com/openai/v1/"
    )


def test_azure_v1_base_url_empty_raises():
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        azure_v1_base_url("  ")


def test_build_llm_azure_returns_retrying_wrapper_by_default(monkeypatch):
    """build_llm('azure', ...) wraps AzureChatAdapter in RetryingChatModelPort."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.delenv("WARDEN_AZURE_USE_RESPONSES_API", raising=False)
    llm = build_llm(
        provider="azure",
        model_name="gpt-5.4-mini-1",
        api_key="azure-fake-key",
    )
    assert isinstance(llm, RetryingChatModelPort)
    assert isinstance(llm, ChatModelPort)
    adapter = _inner_azure(llm)
    assert isinstance(adapter, AzureChatAdapter)
    assert adapter._base_url == "https://example.openai.azure.com/openai/v1/"
    # Chat Completions by default — better Azure prompt-cache behavior than Responses.
    assert adapter._llm.use_responses_api is not True


def test_build_llm_azure_responses_api_opt_in(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("WARDEN_AZURE_USE_RESPONSES_API", "1")
    llm = build_llm(
        provider="azure",
        model_name="gpt-5.4-mini-1",
        api_key="azure-fake-key",
    )
    assert _inner_azure(llm)._llm.use_responses_api is True


def test_build_llm_azure_prompt_cache_retention(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("WARDEN_AZURE_PROMPT_CACHE_RETENTION", "24h")
    llm = build_llm(
        provider="azure",
        model_name="gpt-5.4-mini-1",
        api_key="azure-fake-key",
    )
    assert _inner_azure(llm)._llm.model_kwargs.get("prompt_cache_retention") == "24h"


def test_build_llm_azure_normalizes_provider(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    llm = build_llm(
        provider="AZURE",
        model_name="gpt-5.4-mini-1",
        api_key="azure-fake-key",
    )
    assert isinstance(llm, RetryingChatModelPort)
    assert isinstance(_inner_azure(llm), AzureChatAdapter)


def test_build_llm_azure_missing_endpoint_raises(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        build_llm(
            provider="azure",
            model_name="gpt-5.4-mini-1",
            api_key="azure-fake-key",
        )


def test_build_llm_azure_retry_disabled_returns_bare_adapter(monkeypatch):
    monkeypatch.setenv("WARDEN_LLM_RETRY_ENABLED", "false")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    llm = build_llm(
        provider="azure",
        model_name="gpt-5.4-mini-1",
        api_key="azure-fake-key",
    )
    assert isinstance(llm, AzureChatAdapter)
    assert not isinstance(llm, RetryingChatModelPort)
