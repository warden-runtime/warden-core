"""Unit tests for warden list --watch polling."""

from __future__ import annotations

import pytest
from cli import (
    _IN_FLIGHT_EMPTY_TICKS_REQUIRED,
    _make_saga_watch_stop_fn,
    _step_watch_should_stop,
    app,
)
from typer.testing import CliRunner

_runner = CliRunner()


@pytest.fixture(autouse=True)
def _watch_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cli._watch_stdout_is_tty", lambda: True)


def test_step_watch_should_stop_when_all_terminal():
    items = [{"status": "COMPLETED"}, {"status": "SKIPPED"}]
    assert _step_watch_should_stop(items) is True


def test_step_watch_should_stop_false_while_in_progress():
    items = [{"status": "IN_PROGRESS"}]
    assert _step_watch_should_stop(items) is False


def test_step_watch_should_stop_false_when_empty():
    assert _step_watch_should_stop([]) is False


def test_saga_watch_stop_fn_trace_id_terminal():
    should_stop = _make_saga_watch_stop_fn(trace_id="abc", in_flight=False)
    items = [{"status": "COMPLETED", "trace_id": "abc"}]
    assert should_stop(items) is True


def test_saga_watch_stop_fn_trace_id_still_running():
    should_stop = _make_saga_watch_stop_fn(trace_id="abc", in_flight=False)
    items = [{"status": "RUNNING", "trace_id": "abc"}]
    assert should_stop(items) is False


def test_saga_watch_stop_fn_in_flight_requires_two_empty_ticks():
    should_stop = _make_saga_watch_stop_fn(trace_id=None, in_flight=True)
    assert should_stop([]) is False
    assert should_stop([]) is True


def test_saga_watch_stop_fn_in_flight_resets_empty_streak_when_rows_return():
    should_stop = _make_saga_watch_stop_fn(trace_id=None, in_flight=True)
    assert should_stop([]) is False
    assert should_stop([{"status": "RUNNING"}]) is False
    assert should_stop([]) is False
    assert should_stop([]) is True


def test_in_flight_empty_ticks_required_is_two():
    assert _IN_FLIGHT_EMPTY_TICKS_REQUIRED == 2


def test_list_steps_watch_rejects_json():
    result = _runner.invoke(
        app,
        [
            "list",
            "steps",
            "--trace-id",
            "a" * 32,
            "--watch",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert "--watch cannot be combined with --json" in result.stderr


def test_list_steps_watch_rejects_non_tty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("cli._watch_stdout_is_tty", lambda: False)
    result = _runner.invoke(
        app,
        [
            "list",
            "steps",
            "--trace-id",
            "a" * 32,
            "--watch",
        ],
    )
    assert result.exit_code == 1
    assert "stdout is not a TTY" in result.stderr


def test_list_steps_watch_stops_on_completed(monkeypatch: pytest.MonkeyPatch):
    responses = [
        {"items": [{"order_index": 0, "step_id": "greet", "status": "IN_PROGRESS"}]},
        {"items": [{"order_index": 0, "step_id": "greet", "status": "COMPLETED"}]},
    ]

    def _fake_fetch(_path: str, *, params=None):
        return responses.pop(0)

    monkeypatch.setattr("cli._fetch_engine_get_json", _fake_fetch)
    monkeypatch.setattr("cli.time.sleep", lambda _s: None)
    result = _runner.invoke(
        app,
        [
            "list",
            "steps",
            "--trace-id",
            "b" * 32,
            "--watch",
            "--interval",
            "0.01",
        ],
    )
    assert result.exit_code == 0
    assert "IN_PROGRESS" in result.stdout
    assert "COMPLETED" in result.stdout
    assert "watch tick 1" in result.stdout
    assert "watch tick 2" in result.stdout


def test_list_sagas_watch_stops_on_completed_trace(monkeypatch: pytest.MonkeyPatch):
    responses = [
        {"items": [{"namespace": "default", "trace_id": "c" * 32, "status": "RUNNING"}]},
        {"items": [{"namespace": "default", "trace_id": "c" * 32, "status": "COMPLETED"}]},
    ]

    def _fake_fetch(_path: str, *, params=None):
        return responses.pop(0)

    monkeypatch.setattr("cli._fetch_engine_get_json", _fake_fetch)
    monkeypatch.setattr("cli.time.sleep", lambda _s: None)
    trace_id = "c" * 32
    result = _runner.invoke(
        app,
        [
            "list",
            "sagas",
            "--trace-id",
            trace_id,
            "--watch",
            "--interval",
            "0.01",
        ],
    )
    assert result.exit_code == 0
    assert "RUNNING" in result.stdout
    assert "COMPLETED" in result.stdout


def test_list_sagas_unfiltered_watch_times_out(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("cli._UNFILTERED_WATCH_MAX_S", 1.0)
    clock = {"t": 0.0}

    def _monotonic() -> float:
        return clock["t"]

    def _sleep(interval: float) -> None:
        clock["t"] += interval

    monkeypatch.setattr("cli._fetch_engine_get_json", lambda _path, *, params=None: {"items": []})
    monkeypatch.setattr("cli.time.monotonic", _monotonic)
    monkeypatch.setattr("cli.time.sleep", _sleep)
    result = _runner.invoke(
        app,
        [
            "list",
            "sagas",
            "--watch",
            "--interval",
            "0.5",
        ],
    )
    assert result.exit_code == 1
    assert "exceeded maximum duration" in result.stderr


def test_list_watch_interval_must_be_positive():
    result = _runner.invoke(
        app,
        [
            "list",
            "steps",
            "--trace-id",
            "d" * 32,
            "--watch",
            "--interval",
            "-1",
        ],
    )
    assert result.exit_code == 1
    assert "--interval must be positive" in result.stderr
