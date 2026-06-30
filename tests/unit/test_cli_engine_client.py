"""Unit tests for common.cli_engine_client HTTP/CLI helpers."""

from __future__ import annotations

import io

import httpx
import pytest
import typer
from common.cli_engine_client import (
    format_api_detail,
    handle_request_error,
    require_engine_url,
    say,
)
from common.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_format_api_detail_string_detail():
    resp = httpx.Response(400, json={"detail": "manifest not found"})
    assert format_api_detail(resp) == "manifest not found"


def test_format_api_detail_structured_detail():
    payload = [{"loc": ["body", "name"], "msg": "field required"}]
    resp = httpx.Response(422, json={"detail": payload})
    assert '"field required"' in format_api_detail(resp)


def test_format_api_detail_non_json_body():
    resp = httpx.Response(502, text="Bad Gateway\nfrom proxy")
    assert format_api_detail(resp) == "Bad Gateway from proxy"


def test_format_api_detail_empty_body():
    resp = httpx.Response(500, text="   ")
    assert format_api_detail(resp) == "(empty response body)"


def test_require_engine_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setattr(
        "common.cli_engine_client.get_settings",
        lambda: Settings(engine_url="http://127.0.0.1:8000/"),
    )
    assert require_engine_url() == "http://127.0.0.1:8000"


def test_require_engine_url_exits_when_unset(monkeypatch, capsys):
    monkeypatch.setattr(
        "common.cli_engine_client.get_settings",
        lambda: Settings(engine_url=None),
    )
    with pytest.raises(typer.Exit) as exc_info:
        require_engine_url()
    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "ENGINE_URL is required" in err
    assert "direct DB access is not supported" in err


def test_handle_request_error_timeout_hint(capsys):
    handle_request_error(
        httpx.ReadTimeout("Request timed out"),
        verb="GET",
        path="/v1/health",
    )
    err = capsys.readouterr().err
    assert "GET /v1/health failed" in err
    assert "Increase timeout or check engine load" in err


def test_handle_request_error_connection_hint(capsys):
    handle_request_error(
        httpx.ConnectError("Connection refused"),
        verb="POST",
        path="/v1/sagas/start",
    )
    err = capsys.readouterr().err
    assert "POST /v1/sagas/start failed" in err
    assert "Is the engine listening?" in err


def test_handle_request_error_dns_hint(capsys):
    handle_request_error(
        httpx.ConnectError("[Errno -2] Name or service not known"),
        verb="GET",
        path="/v1/sagas",
    )
    err = capsys.readouterr().err
    assert "DNS lookup failed" in err


def test_say_writes_to_custom_stream_without_color():
    buf = io.StringIO()
    say("ERROR", "something broke", stream=buf)
    assert buf.getvalue() == "ERROR        something broke\n"
