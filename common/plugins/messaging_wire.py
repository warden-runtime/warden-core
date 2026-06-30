"""Wire outbox producer from the plugin registry messaging factory."""

from __future__ import annotations


def wire_messaging_from_registry() -> None:
    """Bind the outbox global producer to ``registry.messaging.create_producer()``."""
    from common.outbox import set_producer
    from common.plugins.registry import get_registry

    set_producer(get_registry().messaging.create_producer())
