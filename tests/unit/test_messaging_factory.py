"""MessagingFactory wiring for outbox producer and queue consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import common.outbox as outbox_module
import pytest
from common.messaging.postgres import PostgresQueueConsumer, PostgresQueueProducer
from common.outbox import get_producer
from common.plugins import register_messaging_factory, reset_registry
from common.plugins.messaging_wire import wire_messaging_from_registry
from common.plugins.registry import get_registry

if TYPE_CHECKING:
    from common.messaging import MessageQueueConsumer, MessageQueueProducer


@pytest.fixture(autouse=True)
def _isolated_registry():
    reset_registry()
    outbox_module._producer = None
    yield
    reset_registry()
    outbox_module._producer = None


async def _noop_handler(_msg: dict) -> None:
    return None


class _RecordingMessagingFactory:
    def __init__(self) -> None:
        self.producer = object()
        self.last_consumer_args: tuple[str, str, Any, int] | None = None
        self.consumer = object()

    def create_producer(self) -> MessageQueueProducer:
        return self.producer  # type: ignore[return-value]

    def create_consumer(
        self,
        topic: str,
        group_id: str,
        handler: Any,
        *,
        max_in_flight: int = 1,
    ) -> MessageQueueConsumer:
        self.last_consumer_args = (topic, group_id, handler, max_in_flight)
        return self.consumer  # type: ignore[return-value]


def test_default_factory_returns_postgres_implementations():
    reg = get_registry()
    assert isinstance(reg.messaging.create_producer(), PostgresQueueProducer)
    consumer = reg.messaging.create_consumer("topic-a", "group-a", _noop_handler)
    assert isinstance(consumer, PostgresQueueConsumer)


def test_wire_messaging_from_registry_uses_custom_producer():
    factory = _RecordingMessagingFactory()
    register_messaging_factory(factory)
    wire_messaging_from_registry()
    assert get_producer() is factory.producer


def test_custom_factory_create_consumer_records_args():
    factory = _RecordingMessagingFactory()
    register_messaging_factory(factory)
    consumer = get_registry().messaging.create_consumer("t", "g", _noop_handler)
    assert consumer is factory.consumer
    assert factory.last_consumer_args == ("t", "g", _noop_handler, 1)


def test_default_factory_passes_max_in_flight_to_consumer():
    consumer = get_registry().messaging.create_consumer("t", "g", _noop_handler, max_in_flight=3)
    assert consumer._max_in_flight == 3
