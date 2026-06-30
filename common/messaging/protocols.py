"""
Ports (ABCs) for the message queue abstraction.
Transport-agnostic; no broker-specific dependencies.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from tortoise.backends.base.client import BaseDBAsyncClient


class MessageQueueProducer(ABC):
    """Port: publish messages to the outbox in the same DB transaction as domain changes."""

    @abstractmethod
    async def publish(
        self,
        topic: str,
        payload: dict,
        *,
        headers: dict | None = None,
        conn: BaseDBAsyncClient | None = None,
    ) -> None:
        """Write the message to the outbox (same transaction as conn when provided).

        Args:
            topic: Destination topic.
            payload: Message body (JSON-serializable dict).
            headers: Optional routing/metadata (e.g. namespace, saga_trace_id).
            conn: DB connection for transactional write; None for separate connection.
        """
        ...


class MessageQueueConsumer(ABC):
    """Port: long-lived loop that pulls messages and invokes an agnostic dictionary handler."""

    def __init__(
        self,
        topic: str,
        group_id: str,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        self.topic = topic
        self.group_id = group_id
        self.handler = handler

    @abstractmethod
    async def start(self) -> None:
        """Run the consumer loop until shutdown."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown."""
        ...
