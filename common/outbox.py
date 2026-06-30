"""
Transactional outbox: high-level emitter that wraps MessageQueueProducer.

Production entrypoints should call ``wire_messaging_from_registry()`` (from
``common.plugins.messaging_wire``) after ``load_plugins_from_env()`` so the
producer comes from ``PluginRegistry.messaging``. Tests may override via
``set_producer()``.
"""

import json
import logging

from opentelemetry.propagate import inject
from tortoise.backends.base.client import BaseDBAsyncClient

from common.contracts import (
    HumanApprovedOutboxPayload,
    HumanRejectedOutboxPayload,
    HumanRetryOutboxPayload,
    SagaEventPayload,
    WorkerCommand,
    WorkerEvent,
)
from common.messaging import MessageQueueProducer

OutboxPayload = (
    WorkerCommand
    | WorkerEvent
    | SagaEventPayload
    | HumanApprovedOutboxPayload
    | HumanRejectedOutboxPayload
    | HumanRetryOutboxPayload
)

logger = logging.getLogger(__name__)

# Default global producer; tests may replace via set_producer (e.g. InMemoryQueueProducer).
_producer: MessageQueueProducer | None = None


def get_producer() -> MessageQueueProducer:
    """Return the global outbox producer.

    Returns:
        The current MessageQueueProducer. If none was set via ``set_producer`` or
        ``wire_messaging_from_registry``, lazily uses
        ``get_registry().messaging.create_producer()``. Never returns None.
    """
    global _producer
    if _producer is None:
        from common.plugins.registry import get_registry

        _producer = get_registry().messaging.create_producer()
    return _producer


def set_producer(producer: MessageQueueProducer) -> None:
    """Override the global producer (e.g. for tests).

    Args:
        producer: Producer instance to use for subsequent get_producer() and
            emit_saga_event. Use None to reset only when explicitly supported.
    """
    global _producer
    _producer = producer


async def emit_saga_event(
    topic: str,
    event_type: str,
    payload_schema: OutboxPayload,
    conn: BaseDBAsyncClient | None = None,
    *,
    idempotency_key: str | None = None,
) -> None:
    """Transactional outbox: build payload and headers from schema, publish via producer.

    Writes to OutboxEvent in the same transaction as conn when conn is provided.
    Injects OpenTelemetry trace context into headers.

    Args:
        topic: Destination topic name.
        event_type: Event type string (stored in headers and for routing).
        payload_schema: Pydantic payload (must have namespace, saga_trace_id;
            step_span_id optional for saga-level events).
        conn: DB connection for transactional publish; None for auto-commit.
        idempotency_key: Optional dedup key; when omitted, uses payload idempotency_key
            or ``{saga_trace_id}:{event_type}:{step_span_id}``.

    Raises:
        ValueError: If payload_schema lacks required routing fields (namespace,
            saga_trace_id).
    """
    carrier = {}
    inject(carrier)

    payload = payload_schema.model_dump(mode="json", exclude_none=True)

    from common.config import get_settings

    settings = get_settings()
    if topic == settings.topic_worker_commands:
        payload_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        if payload_bytes > settings.outbox_max_payload_bytes:
            raise ValueError(
                f"worker-commands payload size {payload_bytes} exceeds "
                f"OUTBOX_MAX_PAYLOAD_BYTES={settings.outbox_max_payload_bytes}"
            )

    try:
        r_namespace = str(payload_schema.namespace)
        r_trace_id = str(payload_schema.saga_trace_id)
    except AttributeError as e:
        logger.error(
            "Payload %s missing required routing fields: %s", payload_schema.__class__.__name__, e
        )
        raise ValueError("Cannot emit outbox event: Payload missing required ID fields.") from e

    r_span_id = getattr(payload_schema, "step_span_id", None)
    step_span_id_header = "" if r_span_id is None else str(r_span_id)

    if idempotency_key is None:
        idempotency_key = getattr(payload_schema, "idempotency_key", None)
    if idempotency_key is None:
        # Orchestrator events: derive key so duplicate emissions dedup.
        idempotency_key = f"{r_trace_id}:{event_type}:{step_span_id_header}"

    headers = {
        "namespace": r_namespace,
        "saga_trace_id": r_trace_id,
        "step_span_id": step_span_id_header,
        "event_type": event_type,
        "idempotency_key": idempotency_key,
        "trace_context": carrier,
    }

    producer = get_producer()
    await producer.publish(topic, payload, headers=headers, conn=conn)
    logger.debug("Outbox event '%s' queued for trace %s", event_type, r_trace_id)
