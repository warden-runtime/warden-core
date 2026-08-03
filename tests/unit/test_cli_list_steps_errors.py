"""Unit tests for warden list steps error surfacing."""

from __future__ import annotations

from cli import app
from typer.testing import CliRunner

_runner = CliRunner()
_TRACE = "a" * 32


def _failed_step_items() -> dict:
    return {
        "items": [
            {
                "order_index": 0,
                "forward_seq": 0,
                "loop_id": None,
                "iteration": None,
                "step_id": "triage",
                "status": "FAILED",
                "step_kind": "reason",
                "step_span_id": "1" * 16,
                "worker": "github-triage",
                "compensates_span_id": None,
                "error_details": {
                    "code": "no_submit_call",
                    "message": "Agent did not call _submit",
                    "last_tool_errors": [
                        {"tool": "list_issues", "preview": "failed to list issues: bad repo"}
                    ],
                },
            }
        ]
    }


def test_list_steps_marks_failed_with_asterisk(monkeypatch):
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: _failed_step_items(),
    )
    result = _runner.invoke(app, ["list", "steps", "--trace-id", _TRACE])
    assert result.exit_code == 0
    assert "FAILED*" in result.stdout
    assert "--errors" in result.stdout
    assert "seq" in result.stdout
    assert "loop" in result.stdout


def test_list_steps_errors_prints_briefs(monkeypatch):
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: _failed_step_items(),
    )
    result = _runner.invoke(app, ["list", "steps", "--trace-id", _TRACE, "--errors"])
    assert result.exit_code == 0
    assert "triage (seq 0):" in result.stdout
    assert "no_submit_call" in result.stdout
    assert "failed to list issues" in result.stdout


def test_list_steps_shows_loop_columns(monkeypatch):
    monkeypatch.setattr(
        "cli._fetch_engine_get_json",
        lambda path, *, params=None: {
            "items": [
                {
                    "order_index": 0,
                    "forward_seq": 0,
                    "loop_id": "refine",
                    "iteration": 1,
                    "step_id": "attempt",
                    "status": "COMPLETED",
                    "step_kind": "reason",
                    "step_span_id": "1" * 16,
                    "worker": "w",
                    "compensates_span_id": None,
                }
            ]
        },
    )
    result = _runner.invoke(app, ["list", "steps", "--trace-id", _TRACE])
    assert result.exit_code == 0
    assert "refine" in result.stdout
    assert "attempt" in result.stdout


def test_list_steps_errors_rejects_json():
    result = _runner.invoke(
        app,
        ["list", "steps", "--trace-id", _TRACE, "--errors", "--json"],
    )
    assert result.exit_code == 1
    assert "--errors" in result.stderr
