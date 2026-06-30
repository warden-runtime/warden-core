"""Unit tests for compensation context hardening helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from common.compensation_context import (
    COMPENSATION_METADATA_KEY,
    build_compensation_metadata,
    compensation_parameter_context,
    is_dirty_forward_step,
    merge_compensation_tool_arguments,
)
from common.models import StepStatus


def test_build_compensation_metadata_blind_cleanup_on_timeout():
    forward = MagicMock()
    forward.span_id = "a" * 16
    forward.step_id = "step-1"
    forward.order_index = 1
    forward.status = StepStatus.TIMED_OUT
    forward.output_payload = None
    forward.pending_review_payload = None
    forward.error_details = {"code": "TIMEOUT"}

    meta = build_compensation_metadata(
        forward,
        undo_span_id="b" * 16,
        idempotency_key="comp-trace-b",
    )
    assert meta["blind_cleanup"] is True
    assert meta["has_forward_output"] is False
    assert meta["dirty_failure"] is True
    assert is_dirty_forward_step(forward) is True


def test_compensation_parameter_context_includes_metadata_when_scheduled():
    saga = MagicMock()
    saga.context = {"input": {"x": 1}, "steps": {}}
    forward = MagicMock()
    forward.span_id = "f" * 16
    forward.step_id = "s1"
    forward.order_index = 0
    forward.status = StepStatus.COMPLETED
    forward.output_payload = {"data": {"id": "42"}}
    forward.pending_review_payload = None
    forward.error_details = None

    ctx = compensation_parameter_context(
        saga,
        forward,
        undo_span_id="u" * 16,
        idempotency_key="comp-t-u",
    )
    assert COMPENSATION_METADATA_KEY in ctx
    assert ctx[COMPENSATION_METADATA_KEY]["blind_cleanup"] is False
    assert ctx["steps"]["s1"]["output"]["data"]["id"] == "42"


def test_compensation_parameter_context_empty_output_layer_on_dirty_step():
    saga = MagicMock()
    saga.context = {"input": {"claim": "C-1"}}
    forward = MagicMock()
    forward.span_id = "f" * 16
    forward.step_id = "s1"
    forward.order_index = 1
    forward.status = StepStatus.TIMED_OUT
    forward.output_payload = None
    forward.pending_review_payload = None
    forward.error_details = {"code": "TIMEOUT"}

    ctx = compensation_parameter_context(
        saga,
        forward,
        undo_span_id="u" * 16,
        idempotency_key="comp-t-u",
    )
    assert ctx["steps"]["s1"]["output"] == {"data": {}}
    assert ctx[COMPENSATION_METADATA_KEY]["blind_cleanup"] is True


@pytest.mark.parametrize(
    ("llm", "original", "key", "expected"),
    [
        ({}, {"a": 1}, "comp-1", {"a": 1, "warden_idempotency_key": "comp-1"}),
        ({"a": None}, {"a": 1}, None, {"a": 1}),
    ],
)
def test_merge_compensation_tool_arguments(llm, original, key, expected):
    assert merge_compensation_tool_arguments(llm, original, idempotency_key=key) == expected
