"""Unit tests for common.config Settings and get_settings."""

from common.config import Settings, get_settings


def test_settings_from_env(monkeypatch):
    """Settings picks up ENGINE_EVENTS_TOPIC and WORKER_COMMANDS_TOPIC from env."""
    monkeypatch.setenv("ENGINE_EVENTS_TOPIC", "my-engine-events")
    monkeypatch.setenv("WORKER_COMMANDS_TOPIC", "my-worker-commands")
    s = Settings()
    assert s.topic_orchestrator_events == "my-engine-events"
    assert s.topic_worker_commands == "my-worker-commands"


def test_get_settings_cached():
    """get_settings() returns the same instance (cached)."""
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b


def test_llm_schema_retry_settings_defaults():
    get_settings.cache_clear()
    s = Settings()
    assert s.llm_schema_retry_enabled is True
    assert s.llm_schema_retry_max_attempts == 3


def test_llm_schema_retry_settings_from_env(monkeypatch):
    monkeypatch.setenv("WARDEN_LLM_SCHEMA_RETRY_ENABLED", "false")
    monkeypatch.setenv("WARDEN_LLM_SCHEMA_RETRY_MAX_ATTEMPTS", "2")
    s = Settings()
    assert s.llm_schema_retry_enabled is False
    assert s.llm_schema_retry_max_attempts == 2
