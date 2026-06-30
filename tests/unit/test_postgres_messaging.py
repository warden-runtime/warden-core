"""Unit tests for Postgres outbox consumer payload assembly."""

import json

from common.messaging.postgres import _assemble_consumer_payload


def test_assemble_consumer_payload_parses_json_strings() -> None:
    row = {
        "payload": json.dumps({"x": 1}),
        "trace_context": json.dumps({"traceparent": "00-ab"}),
        "event_type": "DO_STEP",
        "saga_trace_id": "a" * 32,
        "namespace": "default",
        "step_span_id": "b" * 16,
    }
    payload = _assemble_consumer_payload(row)
    assert payload["x"] == 1
    assert payload["event_type"] == "DO_STEP"
    assert payload["trace_context"] == {"traceparent": "00-ab"}


def test_assemble_consumer_payload_keeps_dict_payload() -> None:
    row = {
        "payload": {"ok": True},
        "trace_context": {},
        "event_type": "SAGA_STARTED",
        "saga_trace_id": "c" * 32,
        "namespace": "ns",
        "step_span_id": "",
    }
    payload = _assemble_consumer_payload(row)
    assert payload["ok"] is True
    assert payload["namespace"] == "ns"
