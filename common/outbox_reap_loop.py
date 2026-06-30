"""Background loop to reap stale outbox rows."""

from __future__ import annotations

import asyncio
import logging

from common.config import get_settings
from common.outbox_reap import run_engine_outbox_maintenance_tick, run_outbox_maintenance_tick
from common.topics import TOPIC_ORCHESTRATOR_EVENTS, TOPIC_WORKER_COMMANDS

logger = logging.getLogger(__name__)


async def run_outbox_reap_loop(shutdown: asyncio.Event, *, engine_only: bool = False) -> None:
    """Periodically reap stale IN_PROGRESS outbox rows until shutdown is set."""
    settings = get_settings()
    interval = settings.outbox_reap_interval_seconds
    while not shutdown.is_set():
        try:
            if engine_only:
                reaped = await run_engine_outbox_maintenance_tick(
                    engine_events_topic=TOPIC_ORCHESTRATOR_EVENTS,
                )
            else:
                reaped = await run_outbox_maintenance_tick(
                    worker_commands_topic=TOPIC_WORKER_COMMANDS,
                    engine_events_topic=TOPIC_ORCHESTRATOR_EVENTS,
                )
            if reaped:
                logger.info("Outbox reap reset %d stale row(s) to PENDING", reaped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox reap failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except TimeoutError:
            pass
