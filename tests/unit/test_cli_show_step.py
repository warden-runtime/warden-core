"""Unit tests for warden show step."""

from __future__ import annotations

import json

from cli import app
from typer.testing import CliRunner

_runner = CliRunner()
_TRACE = "a" * 32
_SPAN = "1" * 16


def test_show_step_json(monkeypatch):
    detail = {
        "step_id": "greet",
        "status": "COMPLETED",
        "order_index": 0,
        "step_span_id": _SPAN,
        "step_kind": "reason",
        "resolved_arguments": {"name": "Ada"},
        "output_payload": {"output": {"data": {"greeting": "Hello, Ada!"}}},
        "prompt_ref": "mock-greet.j2",
    }

    def _fake_fetch(path: str, *, params=None):
        assert path == f"/v1/sagas/{_TRACE}/steps/{_SPAN}"
        return detail

    monkeypatch.setattr("cli._fetch_engine_get_json", _fake_fetch)
    result = _runner.invoke(app, ["show", "step", _TRACE, _SPAN])
    assert result.exit_code == 0
    assert "Hello, Ada!" in result.stdout


def test_show_step_json_flag(monkeypatch):
    detail = {"step_id": "greet", "output_payload": {"big": "x" * 9000}}

    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: detail,
    )
    result = _runner.invoke(app, ["show", "step", _TRACE, _SPAN, "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["step_id"] == "greet"
    assert len(parsed["output_payload"]["big"]) == 9000


def test_show_step_truncates_large_payload(monkeypatch):
    detail = {
        "step_id": "triage",
        "status": "COMPLETED",
        "order_index": 0,
        "step_span_id": _SPAN,
        "step_kind": "reason",
        "output_payload": {"transcript": ["line"] * 2000, "output": {"data": {"k": "v"}}},
    }
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: detail,
    )
    result = _runner.invoke(app, ["show", "step", _TRACE, _SPAN])
    assert result.exit_code == 0
    assert "truncated" in result.stdout
    assert "output.data" in result.stdout


def test_show_step_raw_prints_full_payload(monkeypatch):
    detail = {
        "step_id": "triage",
        "status": "COMPLETED",
        "order_index": 0,
        "step_span_id": _SPAN,
        "step_kind": "reason",
        "output_payload": {"transcript": ["line"] * 50},
    }
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: detail,
    )
    result = _runner.invoke(app, ["show", "step", _TRACE, _SPAN, "--raw"])
    assert result.exit_code == 0
    assert "truncated" not in result.stdout
    assert "transcript" in result.stdout


def test_show_step_by_step_id_resolves_span(monkeypatch):
    calls: list[str] = []

    def _fake_fetch(path: str, *, params=None):
        calls.append(path)
        if path == "/v1/sagas/steps":
            return {
                "items": [
                    {
                        "step_id": "greet",
                        "step_span_id": _SPAN,
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "compensates_span_id": None,
                    }
                ]
            }
        return {
            "step_id": "greet",
            "status": "COMPLETED",
            "order_index": 0,
            "step_span_id": _SPAN,
            "step_kind": "reason",
        }

    monkeypatch.setattr("cli._fetch_engine_get_json", _fake_fetch)
    result = _runner.invoke(app, ["show", "step", _TRACE, "--step-id", "greet"])
    assert result.exit_code == 0
    assert calls[-1] == f"/v1/sagas/{_TRACE}/steps/{_SPAN}"


def test_show_step_by_step_id_disambiguates(monkeypatch):
    def _fake_fetch(path: str, *, params=None):
        if path == "/v1/sagas/steps":
            return {
                "items": [
                    {
                        "step_id": "greet",
                        "step_span_id": "aaaaaaaaaaaaaaaa",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "compensates_span_id": None,
                    },
                    {
                        "step_id": "greet",
                        "step_span_id": "bbbbbbbbbbbbbbbb",
                        "started_at": "2026-06-01T00:00:00+00:00",
                        "compensates_span_id": None,
                    },
                ]
            }
        return {
            "step_id": "greet",
            "status": "COMPLETED",
            "order_index": 0,
            "step_span_id": "bbbbbbbbbbbbbbbb",
            "step_kind": "reason",
        }

    monkeypatch.setattr("cli._fetch_engine_get_json", _fake_fetch)
    result = _runner.invoke(app, ["show", "step", _TRACE, "--step-id", "greet"])
    assert result.exit_code == 0
    assert "2 rows matched" in result.stderr
    assert "bbbbbbbbbbbbbbbb" in result.stderr


def test_show_step_rejects_both_identifiers():
    result = _runner.invoke(
        app,
        ["show", "step", _TRACE, _SPAN, "--step-id", "greet"],
    )
    assert result.exit_code == 1
    assert "not both" in result.stderr


def test_show_step_requires_identifier():
    result = _runner.invoke(app, ["show", "step", _TRACE])
    assert result.exit_code == 1
    assert "step_span_id or --step-id" in result.stderr


def test_show_step_failed_shows_error_details(monkeypatch):
    detail = {
        "step_id": "greet",
        "status": "FAILED",
        "order_index": 0,
        "step_span_id": _SPAN,
        "step_kind": "reason",
        "error_details": {"code": "TOOL_ERROR", "message": "boom"},
    }
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: detail,
    )
    result = _runner.invoke(app, ["show", "step", _TRACE, _SPAN])
    assert result.exit_code == 0
    assert "failure:" in result.stdout
    assert "error_details" in result.stdout
    assert "TOOL_ERROR" in result.stdout


def test_show_step_handles_string_output_payload(monkeypatch):
    detail = {
        "step_id": "greet",
        "status": "COMPLETED",
        "order_index": 0,
        "step_span_id": _SPAN,
        "step_kind": "reason",
        "output_payload": "plain-text-model-output",
    }
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: detail,
    )
    result = _runner.invoke(app, ["show", "step", _TRACE, _SPAN])
    assert result.exit_code == 0
    assert "plain-text-model-output" in result.stdout


def test_show_step_handles_non_dict_output_data(monkeypatch):
    detail = {
        "step_id": "greet",
        "status": "COMPLETED",
        "order_index": 0,
        "step_span_id": _SPAN,
        "step_kind": "reason",
        "output_payload": {"output": {"data": "just a string greeting"}},
    }
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: detail,
    )
    result = _runner.invoke(app, ["show", "step", _TRACE, _SPAN])
    assert result.exit_code == 0
    assert "just a string greeting" in result.stdout


def test_show_step_by_step_id_tiebreaks_on_span_id(monkeypatch):
    same_ts = "2026-01-01T00:00:00+00:00"

    def _fake_fetch(path: str, *, params=None):
        if path == "/v1/sagas/steps":
            return {
                "items": [
                    {
                        "step_id": "greet",
                        "step_span_id": "aaaaaaaaaaaaaaaa",
                        "started_at": same_ts,
                        "compensates_span_id": None,
                    },
                    {
                        "step_id": "greet",
                        "step_span_id": "bbbbbbbbbbbbbbbb",
                        "started_at": same_ts,
                        "compensates_span_id": None,
                    },
                ]
            }
        return {
            "step_id": "greet",
            "status": "COMPLETED",
            "order_index": 0,
            "step_span_id": "bbbbbbbbbbbbbbbb",
            "step_kind": "reason",
        }

    monkeypatch.setattr("cli._fetch_engine_get_json", _fake_fetch)
    result = _runner.invoke(app, ["show", "step", _TRACE, "--step-id", "greet"])
    assert result.exit_code == 0
    assert "bbbbbbbbbbbbbbbb" in result.stderr or "bbbbbbbbbbbbbbbb" in result.stdout
