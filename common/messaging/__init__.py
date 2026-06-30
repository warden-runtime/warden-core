"""
Message queue abstraction: ports and adapters.

Default implementation: Postgres-backed outbox (`OutboxEvent`) with SKIP LOCKED polling.
"""

from common.messaging.postgres import PostgresQueueConsumer, PostgresQueueProducer
from common.messaging.protocols import MessageQueueConsumer, MessageQueueProducer

__all__ = [
    "MessageQueueProducer",
    "MessageQueueConsumer",
    "PostgresQueueProducer",
    "PostgresQueueConsumer",
]
