"""Unit tests for common.telemetry tag safety helpers."""

from common.telemetry import (
    _MAX_SPAN_TAG_LEN,
    _MAX_TRACE_CONTEXT_JSON_BYTES,
    _parse_trace_context_value,
    safe_truncate_tag,
)


def test_safe_truncate_tag_leaves_short_values() -> None:
    assert safe_truncate_tag("ok") == "ok"


def test_safe_truncate_tag_truncates_long_values() -> None:
    raw = "x" * (_MAX_SPAN_TAG_LEN + 10)
    truncated = safe_truncate_tag(raw)
    assert truncated.endswith("...[TRUNCATED]")
    assert len(truncated) == _MAX_SPAN_TAG_LEN + len("...[TRUNCATED]")


def test_parse_trace_context_value_rejects_oversized_json_string() -> None:
    oversized = "{" + ("a" * (_MAX_TRACE_CONTEXT_JSON_BYTES + 1)) + "}"
    assert _parse_trace_context_value(oversized) is None


def test_parse_trace_context_value_accepts_dict() -> None:
    assert _parse_trace_context_value({"traceparent": "00"}) == {"traceparent": "00"}
