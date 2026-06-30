"""Unit-test fixtures for tests/unit only (not loaded by postgres/integration suites)."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once


@pytest.fixture
def memory_span_exporter():
    """Swap in an in-memory TracerProvider for one test, then restore globals."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    previous_provider = trace._TRACER_PROVIDER
    previous_once = trace._TRACER_PROVIDER_SET_ONCE
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._set_tracer_provider(provider, log=False)
    exporter.clear()

    try:
        yield exporter
    finally:
        exporter.clear()
        trace._TRACER_PROVIDER = previous_provider
        trace._TRACER_PROVIDER_SET_ONCE = previous_once
