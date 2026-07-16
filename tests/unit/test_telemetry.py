"""Unit tests for common.telemetry OTLP wiring, log envelope, and span timing."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import MagicMock

import pytest
from common.config import Settings, get_settings
from common.execution_timing import WorkerTimingAccumulator
from common.telemetry import (
    _JsonLogFormatter,
    _otlp_span_exporter,
    _tag_span_from_dict,
    _WardenLogContextFilter,
    get_bound_log_context,
    log_context,
    record_timing_bucket_on_current_span,
    resolve_logging_level,
    run_in_executor_with_log_context,
    setup_telemetry,
)
from opentelemetry import trace


def test_record_timing_bucket_on_current_span_sets_attribute(memory_span_exporter):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("worker_cmd"):
        record_timing_bucket_on_current_span(section="worker", bucket="tool_ms", ms=42)
    spans = memory_span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["timing.worker.tool_ms"] == 42


def test_record_timing_bucket_skips_zero_and_invalid_bucket(memory_span_exporter):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("noop"):
        record_timing_bucket_on_current_span(section="worker", bucket="tool_ms", ms=0)
        record_timing_bucket_on_current_span(section="engine", bucket="not_a_bucket", ms=10)
    spans = memory_span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes.get("timing.worker.tool_ms") is None
    assert spans[0].attributes.get("timing.engine.not_a_bucket") is None


def test_record_timing_bucket_noop_without_recording_span():
    record_timing_bucket_on_current_span(section="worker", bucket="tool_ms", ms=99)


def test_worker_accumulator_cumulative_mirror_on_span(memory_span_exporter):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("react"):
        acc = WorkerTimingAccumulator()
        acc.add_ms("llm_ms", 60)
        acc.add_ms("llm_ms", 40)
    spans = memory_span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["timing.worker.llm_ms"] == 100


@pytest.mark.asyncio
async def test_concurrent_timing_mirror_isolation(memory_span_exporter):
    memory_span_exporter.clear()
    tracer = trace.get_tracer("test")

    async def mirror_task(span_name: str, policy_ms: int) -> None:
        with tracer.start_as_current_span(span_name):
            record_timing_bucket_on_current_span(
                section="engine",
                bucket="policy_ms",
                ms=policy_ms,
            )
        await asyncio.sleep(0.005)

    await asyncio.gather(mirror_task("ingest_task_a", 11), mirror_task("ingest_task_b", 22))

    by_name = {span.name: span for span in memory_span_exporter.get_finished_spans()}
    assert by_name["ingest_task_a"].attributes["timing.engine.policy_ms"] == 11
    assert by_name["ingest_task_b"].attributes["timing.engine.policy_ms"] == 22


def test_otlp_span_exporter_uses_settings_endpoint_and_insecure(mocker, monkeypatch):
    monkeypatch.setenv("OTLP_ENDPOINT", "http://jaeger:4317")
    monkeypatch.setenv("OTLP_INSECURE", "false")
    get_settings.cache_clear()
    mock_cls = mocker.patch("common.telemetry.OTLPSpanExporter")
    _otlp_span_exporter()
    mock_cls.assert_called_once_with(endpoint="http://jaeger:4317", insecure=False)


def test_otlp_span_exporter_omits_endpoint_when_unset(mocker, monkeypatch):
    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    get_settings.cache_clear()
    mock_cls = mocker.patch("common.telemetry.OTLPSpanExporter")
    _otlp_span_exporter()
    mock_cls.assert_called_once_with(insecure=True)


def test_settings_otlp_defaults():
    s = Settings()
    assert s.otlp_endpoint is None
    assert s.otlp_insecure is True
    assert s.logging_level == "INFO"


def test_resolve_logging_level_from_settings(monkeypatch):
    monkeypatch.setenv("LOGGING_LEVEL", "DEBUG")
    get_settings.cache_clear()
    assert resolve_logging_level() == logging.DEBUG
    get_settings.cache_clear()


def test_json_formatter_includes_warden_log_fields():
    fmt = _JsonLogFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.service_name = "worker-node"
    record.warden_trace_id = "aabb" * 8
    record.warden_span_id = "ccdd" * 4
    record.warden_step_id = "step1"
    payload = json.loads(fmt.format(record))
    assert payload["trace_id"] == "aabb" * 8
    assert payload["span_id"] == "ccdd" * 4
    assert payload["step_id"] == "step1"
    assert payload["level"] == "INFO"
    assert payload["service"] == "worker-node"


def test_warden_log_context_filter_copies_contextvars():
    filt = _WardenLogContextFilter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    with log_context(trace_id="t1", span_id="s1", step_id="step1"):
        assert filt.filter(record) is True
    assert record.warden_trace_id == "t1"
    assert record.warden_span_id == "s1"
    assert record.warden_step_id == "step1"


def test_tag_span_from_dict_separates_manifest_step_id(memory_span_exporter):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("tagged") as span:
        _tag_span_from_dict(
            span,
            {
                "saga_trace_id": "trace-1",
                "step_span_id": "span-1",
                "step_id": "greet",
            },
        )
    attrs = memory_span_exporter.get_finished_spans()[0].attributes
    assert attrs["saga.id"] == "trace-1"
    assert attrs["saga.step_span_id"] == "span-1"
    assert attrs["saga.step_id"] == "greet"


@pytest.mark.asyncio
async def test_run_in_executor_with_log_context_propagates_binding():
    with log_context(trace_id="exec-trace", span_id="exec-span", step_id="exec-step"):

        def _read_bound() -> dict[str, str | None]:
            return get_bound_log_context()

        bound = await run_in_executor_with_log_context(_read_bound)
    assert bound == {
        "trace_id": "exec-trace",
        "span_id": "exec-span",
        "step_id": "exec-step",
    }


def test_setup_telemetry_builds_provider_with_configured_exporter(mocker, monkeypatch):
    monkeypatch.setenv("OTLP_ENDPOINT", "http://collector:4317")
    get_settings.cache_clear()
    mock_exporter = mocker.patch("common.telemetry._otlp_span_exporter", return_value=MagicMock())
    mock_provider_cls = mocker.patch("common.telemetry.TracerProvider")
    mocker.patch("common.telemetry.BatchSpanProcessor")
    mocker.patch("common.telemetry.trace.set_tracer_provider")

    setup_telemetry("engine-node")

    mock_exporter.assert_called_once()
    mock_provider_cls.return_value.add_span_processor.assert_called_once()
