import asyncio
import atexit
import contextlib
import contextvars
import inspect
import json
import logging
import logging.handlers
import queue
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

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
_NOISY_LOGGERS = (
    "tortoise",
    "asyncio",
    "httpx",
    "httpcore",
    "openai",
    "anthropic",
    "urllib3",
    "opentelemetry",
)

_cv_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "warden_log_trace_id", default=None
)
_cv_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "warden_log_span_id", default=None
)
_cv_step_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "warden_log_step_id", default=None
)

T = TypeVar("T")


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
USAGE_WORKER_ATTR_PREFIX = "usage.worker."


def resolve_logging_level(level: int | str | None = None) -> int:
    """Resolve LOGGING_LEVEL / Settings into a stdlib logging level int."""
    if level is None:
        from common.config import get_settings

        level = get_settings().logging_level
    if isinstance(level, int):
        return level
    name = str(level).strip().upper()
    resolved = logging.getLevelNamesMapping().get(name)
    if resolved is None:
        logger.warning("Unknown LOGGING_LEVEL %r; defaulting to INFO", level)
        return logging.INFO
    return resolved


def bind_log_context(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    step_id: str | None = None,
) -> None:
    """Bind Warden ledger / manifest IDs into the current context for JSON logs."""
    if trace_id is not None:
        _cv_trace_id.set(str(trace_id) or None)
    if span_id is not None:
        _cv_span_id.set(str(span_id) or None)
    if step_id is not None:
        _cv_step_id.set(str(step_id) or None)


def clear_log_context() -> None:
    """Clear bound Warden log context (call from ``finally`` after a handler)."""
    _cv_trace_id.set(None)
    _cv_span_id.set(None)
    _cv_step_id.set(None)


@contextlib.contextmanager
def log_context(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    step_id: str | None = None,
) -> Iterator[None]:
    """Temporarily bind ledger IDs for structured logs; reset on exit."""
    t_trace = _cv_trace_id.set(str(trace_id) if trace_id else None)
    t_span = _cv_span_id.set(str(span_id) if span_id else None)
    t_step = _cv_step_id.set(str(step_id) if step_id else None)
    try:
        yield
    finally:
        _cv_trace_id.reset(t_trace)
        _cv_span_id.reset(t_span)
        _cv_step_id.reset(t_step)


def get_bound_log_context() -> dict[str, str | None]:
    """Return currently bound Warden log fields (for tests / debugging)."""
    return {
        "trace_id": _cv_trace_id.get(),
        "span_id": _cv_span_id.get(),
        "step_id": _cv_step_id.get(),
    }


async def run_in_executor_with_log_context(
    func: Callable[..., T],
    /,
    *args: Any,
    executor: Any = None,
    **kwargs: Any,
) -> T:
    """Run ``func`` in a thread pool with a copied contextvars snapshot.

    Required when background work must emit logs under the same Warden
    ``trace_id`` / ``span_id`` / ``step_id`` binding as the caller. Contextvars do
    not propagate to new threads automatically::

        await run_in_executor_with_log_context(your_logging_sandbox_task)
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()

    def _call() -> T:
        return ctx.run(func, *args, **kwargs)

    return await loop.run_in_executor(executor, _call)


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


def record_usage_counter_on_current_span(
    *,
    section: Literal["worker"],
    key: str,
    value: int,
) -> None:
    """Mirror a cumulative worker usage counter onto the active OTel span.

    Same call-site rules as ``record_timing_bucket_on_current_span``. Pass the
    **running total** for the counter (not the delta).
    """
    from common.execution_timing import clamp_nonneg
    from common.execution_usage import WORKER_USAGE_COUNTERS

    if section != "worker":
        return
    cumulative = clamp_nonneg(int(value))
    if cumulative <= 0 or key not in WORKER_USAGE_COUNTERS:
        return
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attribute(f"{USAGE_WORKER_ATTR_PREFIX}{key}", cumulative)


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


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _log_fields_from_payload(payload: dict) -> dict[str, str | None]:
    return {
        "trace_id": _as_optional_str(payload.get("saga_trace_id") or payload.get("saga_id")),
        "span_id": _as_optional_str(payload.get("step_span_id")),
        "step_id": _as_optional_str(payload.get("step_id")),
    }


class _ServiceNameFilter(logging.Filter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.service_name = self._service_name
        return True


class _WardenLogContextFilter(logging.Filter):
    """Copy contextvars onto the LogRecord at emit time (QueueHandler emission thread)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.warden_trace_id = _cv_trace_id.get()
        record.warden_span_id = _cv_span_id.get()
        record.warden_step_id = _cv_step_id.get()
        return True


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": getattr(record, "service_name", None),
            "trace_id": getattr(record, "warden_trace_id", None),
            "span_id": getattr(record, "warden_span_id", None),
            "step_id": getattr(record, "warden_step_id", None),
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


def _quiet_third_party_loggers(*, root_level: int) -> None:
    # Keep third-party noise off stderr unless the process is DEBUG.
    library_level = logging.DEBUG if root_level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(library_level)


def configure_logging(service_name: str, *, level: int | str | None = None) -> None:
    """Configure non-blocking structured JSON logging on the **root** logger.

    Hijacks ``logging.getLogger()`` (root): removes pre-existing handlers so
    third-party libraries cannot emit raw text to stderr alongside JSON lines.
    Uses a ``QueueHandler`` so emitters on the asyncio event loop do not block on
    stderr I/O. Safe to call once per process; subsequent calls only refresh level.
    """
    global _log_listener
    resolved_level = resolve_logging_level(level)

    root = logging.getLogger()
    root.setLevel(resolved_level)
    _quiet_third_party_loggers(root_level=resolved_level)

    if _log_listener is not None:
        return

    for handler in list(root.handlers):
        root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    # Filter runs on the emitting task so contextvars are still correct before
    # the record is formatted on the QueueListener thread.
    queue_handler.addFilter(_WardenLogContextFilter())
    root.addHandler(queue_handler)

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
    binds Warden log context, and re-raises after recording exceptions.

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
                log_fields: dict[str, str | None] = {
                    "trace_id": None,
                    "span_id": None,
                    "step_id": None,
                }
                if isinstance(event_data, dict):
                    _tag_span_from_dict(span, event_data)
                    log_fields = _log_fields_from_payload(event_data)

                with log_context(**log_fields):
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
    step_span_id = payload.get("step_span_id")
    if step_span_id:
        _set_truncated_attribute(span, "saga.step_span_id", step_span_id)
    manifest_step_id = payload.get("step_id")
    if manifest_step_id:
        _set_truncated_attribute(span, "saga.step_id", manifest_step_id)
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
    # Manifest step_id is optional on ingest models — tag when present.
    if "step_id" in model_type.model_fields:
        step_id = getattr(model, "step_id", None)
        if step_id:
            _set_truncated_attribute(span, "saga.step_id", step_id)


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
    _set_truncated_attribute(span, "saga.step_id", step.step_id)
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


def _merge_log_fields_from_value(
    fields: dict[str, str | None], kind: _SpanTagKind, val: object
) -> None:
    if kind is _SpanTagKind.SAGA and _type_identity(val) == _SAGA_INSTANCE:
        saga = cast("SagaInstance", val)
        fields["trace_id"] = _as_optional_str(saga.trace_id)
        return
    if kind is _SpanTagKind.STEP and _type_identity(val) == _STEP_INSTANCE:
        step = cast("SagaStepInstance", val)
        fields["trace_id"] = _as_optional_str(step.saga_trace_id)
        fields["span_id"] = _as_optional_str(step.span_id)
        fields["step_id"] = _as_optional_str(step.step_id)
        return
    if kind is _SpanTagKind.EVENT and isinstance(val, BaseModel):
        fields.update(_log_fields_from_payload(val.model_dump(mode="json")))
        return
    if kind is _SpanTagKind.TRACE_CONTEXT and isinstance(val, dict):
        fields.update(_log_fields_from_payload(val))


def _log_fields_from_tag_slots(
    slots: tuple[_SpanTagSlot, ...], args: tuple, kwargs: dict
) -> dict[str, str | None]:
    fields: dict[str, str | None] = {"trace_id": None, "span_id": None, "step_id": None}
    for slot in slots:
        val = _value_for_slot(slot, args, kwargs)
        if val is not None:
            _merge_log_fields_from_value(fields, slot.kind, val)
    return fields


def trace_step(span_name: str | None = None):
    """Decorator for internal async functions: child span with auto-tagging.

    Creates a child span (inherits OTel context). Inspects args/kwargs for
    saga/step/worker IDs and event_type and sets span attributes. Binds Warden
    log context for the duration of the call.

    Args:
        span_name: Name for the span; if None, uses the wrapped function name.

    Returns:
        A decorator that wraps an async function (*args, **kwargs).
    """

    def decorator(func):
        tag_slots = _span_tag_slots(func)

        @wraps(func)
        async def wrapper(*args, **kwargs):
            name = span_name or func.__name__

            tracer = trace.get_tracer(func.__module__)
            with tracer.start_as_current_span(name) as span:
                if tag_slots:
                    _apply_tag_slots(span, tag_slots, args, kwargs)
                log_fields = (
                    _log_fields_from_tag_slots(tag_slots, args, kwargs)
                    if tag_slots
                    else {"trace_id": None, "span_id": None, "step_id": None}
                )
                with log_context(**log_fields):
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
