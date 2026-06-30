"""Unit tests for common.plugins.loader (WARDEN_PLUGINS dynamic import)."""

from __future__ import annotations

import importlib

import pytest
from common.plugins import loader


@pytest.fixture(autouse=True)
def _reset_loader_state():
    loader._loaded = False
    yield
    loader._loaded = False


def test_load_plugins_from_env_noop_when_unset(monkeypatch):
    monkeypatch.delenv("WARDEN_PLUGINS", raising=False)
    loader.load_plugins_from_env()
    assert loader._loaded is True


def test_load_plugins_from_env_idempotent(monkeypatch, mocker):
    monkeypatch.setenv("WARDEN_PLUGINS", "tests.fixtures.dummy_plugin:install")
    import tests.fixtures.dummy_plugin as dummy

    dummy._called = False
    spy = mocker.spy(dummy, "install")
    loader.load_plugins_from_env()
    loader.load_plugins_from_env()
    spy.assert_called_once()
    assert dummy._called is True


def test_load_plugins_from_env_valid_entrypoint(monkeypatch):
    monkeypatch.setenv("WARDEN_PLUGINS", "tests.fixtures.dummy_plugin:install")
    import tests.fixtures.dummy_plugin as dummy

    dummy._called = False
    loader.load_plugins_from_env()
    assert dummy._called is True


def test_load_plugins_from_env_malformed_spec_raises(monkeypatch):
    monkeypatch.setenv("WARDEN_PLUGINS", "not-a-valid-spec")
    with pytest.raises(ValueError, match="module.path:callable"):
        loader.load_plugins_from_env()
    assert loader._loaded is False


def test_load_plugins_from_env_missing_callable_raises(monkeypatch):
    monkeypatch.setenv("WARDEN_PLUGINS", "tests.fixtures.dummy_plugin:missing_fn")
    with pytest.raises(AttributeError):
        loader.load_plugins_from_env()
    assert loader._loaded is False


def test_load_plugins_from_env_missing_module_raises(monkeypatch):
    monkeypatch.setenv("WARDEN_PLUGINS", "tests.fixtures.no_such_module:install")
    with pytest.raises(ModuleNotFoundError):
        loader.load_plugins_from_env()
    assert loader._loaded is False


def test_load_plugins_from_env_entrypoint_exception_propagates(monkeypatch):
    monkeypatch.setenv("WARDEN_PLUGINS", "tests.fixtures.dummy_plugin:broken_install")

    mod = importlib.import_module("tests.fixtures.dummy_plugin")

    def broken_install() -> None:
        raise RuntimeError("plugin init failed")

    monkeypatch.setattr(mod, "broken_install", broken_install, raising=False)
    with pytest.raises(RuntimeError, match="plugin init failed"):
        loader.load_plugins_from_env()
    assert loader._loaded is False
