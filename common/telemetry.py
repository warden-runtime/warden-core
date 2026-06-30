import atexit
import inspect
import json
import logging
import logging.handlers
import queue
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal, cast

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import BaseModel

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from common.models import SagaInstance, SagaStepInstance

_INGEST_TAG_FIELDS = frozenset({"saga_trace_id", "namespace", "event_type", "step_span_id"})
_SAGA_INSTANCE = ("common.models", "SagaInstance")
_STEP_INSTANCE = ("common.models", "SagaStepInstance")
_MAX_SPAN_TAG_LEN = 512
_MAX_TRACE_CONTEXT_JSON_BYTES = 65536
_FIELD_TO_SPAN_ATTR = {
    "saga_trace_id": "saga.id",
    "step_span_id": "saga.step_span_id",
    "event_type": "event.type",
    "namespace": "namespace",
}


class _SpanTagKind(Enum):
    SAGA = "saga"
    STEP = "step"
    EVENT = "event"
    TRACE_CONTEXT = "trace_context"


_log_listener: logging.handlers.QueueListener | None = None
_logging_instrumented = False
_fastapi_instrumented = False
_OTEL_LOG_KEYS = ("otelTraceID", "otelSpanID", "otelServiceName", "otelTraceSampled")

TIMING_WORKER_ATTR_PREFIX = "timing.worker."
TIMING_ENGINE_ATTR_PREFIX = "timing.engine."


def record_timing_bucket_on_current_span(
    *,
    section: Literal["worker", "engine"],
    bucket: str,
    ms: int,
) -> None:
    """Mirror a cumulative timing bucket onto the active OTel span.

    Call only from the ``await`` tree of ``@trace_boundary`` / ``@trace_step``
    handlers while a recording span is active. Pass the **running total** for
    the bucket (not the delta).

    Do **not** call from ``engine/recovery.py``, outbox reap loops, enterprise
    governance, or detached ``asyncio.create_task`` work without propagated context.
    """
    from common.execution_timing import ENGINE_BUCKETS, WORKER_BUCKETS, clamp_nonneg

    cumulative = clamp_nonneg(int(ms))
    if cumulative <= 0:
        return
    allowed = WORKER_BUCKETS if section == "worker" else ENGINE_BUCKETS
    if bucket not in allowed:
        return
    span = trace.get_current_span()
    if not span.is_recording():
        return
    prefix = TIMING_WORKER_ATTR_PREFIX if section == "worker" else TIMING_ENGINE_ATTR_PREFIX
    span.set_attribute(f"{prefix}{bucket}", cumulative)


def safe_truncate_tag(value: Any, max_len: int = _MAX_SPAN_TAG_LEN) -> str:
    """Bound span tag and status strings to avoid log/OTel saturation from huge payloads."""
    val_str = str(value)
    if len(val_str) > max_len:
        return val_str[:max_len] + "...[TRUNCATED]"
    return val_str


def _set_truncated_attribute(span: trace.Span, key: str, value: object) -> None:
    span.set_attribute(key, safe_truncate_tag(value))


def _parse_trace_context_value(raw: object) -> dict[str, object] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    if len(raw) > _MAX_TRACE_CONTEXT_JSON_BYTES:
        logger.warning(
            "trace_context JSON exceeds %d bytes; starting fresh trace",
            _MAX_TRACE_CONTEXT_JSON_BYTES,
        )
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not decode trace_context JSON; starting fresh trace")
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_parent_context(event_data: object) -> Context | None:
    if not isinstance(event_data, dict) or "trace_context" not in event_data:
        return None
    ctx_data = _parse_trace_context_value(event_data["trace_context"])
    if ctx_data is None:
        return None
    return extract(ctx_data)


class _ServiceNameFilter(logging.Filter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.service_name = self._service_name
        return True


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": getattr(record, "service_name", None),
        }
        for key in _OTEL_LOG_KEYS:
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _stop_log_listener() -> None:
    global _log_listener
    if _log_listener is not None:
        _log_listener.stop()
        _log_listener = None


def configure_logging(service_name: str, *, level: int = logging.INFO) -> None:
    """Configure non-blocking structured logging aligned with OTel trace context.

    Uses a ``QueueHandler`` so emitters on the asyncio event loop do not block on
    stderr I/O. Safe to call once per process; subsequent calls are no-ops.
    """
    global _log_listener
    if _log_listener is not None:
        return

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    root.addHandler(logging.handlers.QueueHandler(log_queue))

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(_JsonLogFormatter())
    stream_handler.addFilter(_ServiceNameFilter(service_name))

    _log_listener = logging.handlers.QueueListener(
        log_queue,
        stream_handler,
        respect_handler_level=True,
    )
    _log_listener.start()
    atexit.register(_stop_log_listener)

    global _logging_instrumented
    if not _logging_instrumented:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=False)
        _logging_instrumented = True


def trace_boundary(span_name_key: str = "event_type"):
    """Decorator for async worker handlers: OTel context extraction, span, and tagging.

    Extracts trace context from event payload (dict with "trace_context", or
    JSON string), starts a consumer span, tags saga/step/event_type from payload,
    and re-raises after recording exceptions.

    Args:
        span_name_key: Key in event_data (if dict) to use as span name; default
            "event_type". Fallback: wrapped function name.

    Returns:
        A decorator that wraps an async function (event_data, *args, **kwargs).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(event_data, *args, **kwargs):
            parent_context = _extract_parent_context(event_data)

            span_name = (
                event_data.get(span_name_key, func.__name__)
                if isinstance(event_data, dict)
                else func.__name__
            )

            tracer = trace.get_tracer(func.__module__)
            with tracer.start_as_current_span(
                span_name, context=parent_context, kind=trace.SpanKind.CONSUMER
            ) as span:
                if isinstance(event_data, dict):
                    _tag_span_from_dict(span, event_data)

                try:
                    return await func(event_data, *args, **kwargs)
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, safe_truncate_tag(e)))
                    raise e

        return wrapper

    return decorator


def _tag_span_from_dict(span: trace.Span, payload: dict) -> None:
    saga_id = payload.get("saga_trace_id") or payload.get("saga_id")
    if saga_id:
        _set_truncated_attribute(span, "saga.id", saga_id)
    step_id = payload.get("step_span_id") or payload.get("step_id")
    if step_id:
        _set_truncated_attribute(span, "saga.step_span_id", step_id)
    event_type = payload.get("event_type")
    if event_type:
        _set_truncated_attribute(span, "event.type", event_type)
    namespace = payload.get("namespace")
    if namespace:
        _set_truncated_attribute(span, "namespace", namespace)


def _tag_span_from_pydantic(span: trace.Span, model: BaseModel) -> None:
    model_type = type(model)
    if "saga_trace_id" not in model_type.model_fields:
        return
    include = _INGEST_TAG_FIELDS & model_type.model_fields.keys()
    if not include:
        return
    for key, value in model.model_dump(mode="json", include=set(include)).items():
        if not value:
            continue
        attr = _FIELD_TO_SPAN_ATTR.get(key)
        if attr is not None:
            _set_truncated_attribute(span, attr, value)


def _type_identity(val: object) -> tuple[str, str]:
    t = type(val)
    return t.__module__, t.__name__


def _tag_span_from_saga(span: trace.Span, val: object) -> None:
    if _type_identity(val) != _SAGA_INSTANCE:
        return
    saga = cast("SagaInstance", val)
    _set_truncated_attribute(span, "saga.id", saga.trace_id)
    _set_truncated_attribute(span, "namespace", saga.namespace)


def _tag_span_from_step(span: trace.Span, val: object) -> None:
    if _type_identity(val) != _STEP_INSTANCE:
        return
    step = cast("SagaStepInstance", val)
    _set_truncated_attribute(span, "saga.id", step.saga_trace_id)
    _set_truncated_attribute(span, "saga.step_span_id", step.span_id)
    _set_truncated_attribute(span, "namespace", step.namespace)
    _set_truncated_attribute(span, "worker.id", step.worker)


_TAG_HANDLERS: dict[_SpanTagKind, Callable[[trace.Span, object], None]] = {
    _SpanTagKind.SAGA: _tag_span_from_saga,
    _SpanTagKind.STEP: _tag_span_from_step,
}


def _tag_bound_argument(span: trace.Span, kind: _SpanTagKind, val: object) -> None:
    if kind is _SpanTagKind.EVENT and isinstance(val, BaseModel):
        _tag_span_from_pydantic(span, val)
        return
    if kind is _SpanTagKind.TRACE_CONTEXT and isinstance(val, dict):
        _tag_span_from_dict(span, val)
        return
    handler = _TAG_HANDLERS.get(kind)
    if handler is not None:
        handler(span, val)


@dataclass(frozen=True, slots=True)
class _SpanTagSlot:
    """Decoration-time slot: parameter index + name for fast runtime lookup."""

    index: int
    name: str
    kind: _SpanTagKind


def _span_tag_kind(param_name: str) -> _SpanTagKind | None:
    return {
        "saga": _SpanTagKind.SAGA,
        "step": _SpanTagKind.STEP,
        "event": _SpanTagKind.EVENT,
        "trace_context": _SpanTagKind.TRACE_CONTEXT,
    }.get(param_name)


def _span_tag_slots(func: Callable) -> tuple[_SpanTagSlot, ...]:
    slots: list[_SpanTagSlot] = []
    for index, name in enumerate(inspect.signature(func).parameters):
        kind = _span_tag_kind(name)
        if kind is not None:
            slots.append(_SpanTagSlot(index=index, name=name, kind=kind))
    return tuple(slots)


def _value_for_slot(slot: _SpanTagSlot, args: tuple, kwargs: dict) -> object | None:
    if slot.name in kwargs:
        return kwargs[slot.name]
    if slot.index < len(args):
        return args[slot.index]
    return None


def _apply_tag_slots(
    span: trace.Span, slots: tuple[_SpanTagSlot, ...], args: tuple, kwargs: dict
) -> None:
    for slot in slots:
        val = _value_for_slot(slot, args, kwargs)
        if val is not None:
            _tag_bound_argument(span, slot.kind, val)


def trace_step(span_name: str | None = None):
    """Decorator for internal async functions: child span with auto-tagging.

    Creates a child span (inherits OTel context). Inspects args/kwargs for
    saga/step/worker IDs and event_type and sets span attributes.

    Args:
        span_name: Name for the span; if None, uses the wrapped function name.

    Returns:
        A decorator that wraps an async function (*args, **kwargs).
    """

    def decorator(func):
        tag_slots = _span_tag_slots(func)

        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Use function name if no specific name provided
            name = span_name or func.__name__

            tracer = trace.get_tracer(func.__module__)
            with tracer.start_as_current_span(name) as span:
                if tag_slots:
                    _apply_tag_slots(span, tag_slots, args, kwargs)

                # --- EXECUTION ---
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, safe_truncate_tag(e)))
                    raise e

        return wrapper

    return decorator


def instrument_fastapi_app(app: "FastAPI") -> None:
    """Attach OTel trace propagation to a FastAPI app (no-op if instrumentation is unavailable).

    Call once after core routers are registered and before plugin ``http.mount`` adds routes,
    so inbound HTTP spans inherit W3C context before extension handlers run.
    """
    global _fastapi_instrumented
    if _fastapi_instrumented:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.debug("opentelemetry-instrumentation-fastapi not installed; skipping HTTP tracing")
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/v1/health")
    _fastapi_instrumented = True


def _otlp_span_exporter() -> OTLPSpanExporter:
    """Build OTLP gRPC exporter from app settings (endpoint optional; SDK env fallback)."""
    from common.config import get_settings

    settings = get_settings()
    endpoint = (settings.otlp_endpoint or "").strip()
    if endpoint:
        return OTLPSpanExporter(endpoint=endpoint, insecure=settings.otlp_insecure)
    return OTLPSpanExporter(insecure=settings.otlp_insecure)


def setup_telemetry(service_name: str):
    """Configure the OTel SDK (TracerProvider, OTLP gRPC exporter, BatchSpanProcessor).

    OTLP endpoint and TLS mode come from ``OTLP_ENDPOINT`` / ``OTLP_INSECURE`` in Settings.
    When ``OTLP_ENDPOINT`` is unset, the exporter uses OpenTelemetry SDK defaults
    (e.g. ``OTEL_EXPORTER_OTLP_ENDPOINT`` from the environment).

    Args:
        service_name: Used for resource.service.name and as default tracer name.

    Returns:
        The tracer for the given service_name.
    """

    from common.config import get_settings

    resource = Resource.create(
        {"service.name": service_name, "deployment.environment": get_settings().env}
    )

    provider = TracerProvider(resource=resource)
    exporter = _otlp_span_exporter()

    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)

    return trace.get_tracer(service_name)


def get_tracer(name: str):
    """Return the tracer for the given name (from the global provider).

    Args:
        name: Tracer name (e.g. module or service name).

    Returns:
        A Tracer instance.
    """
    return trace.get_tracer(name)
