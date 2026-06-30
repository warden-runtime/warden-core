"""Background loop to reap stale ProcessedCommand claims in the worker process."""

from __future__ import annotations

import asyncio
import logging

from common.config import get_settings
from common.processed_command_reap import reap_stale_processed_commands

logger = logging.getLogger(__name__)


async def run_claim_reap_loop(shutdown: asyncio.Event) -> None:
    """Periodically delete stale claims until shutdown is set."""
    settings = get_settings()
    interval = settings.processed_command_reap_interval_seconds
    while not shutdown.is_set():
        try:
            deleted = await reap_stale_processed_commands()
            if deleted:
                logger.info("Claim reap removed %d stale ProcessedCommand row(s)", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ProcessedCommand claim reap failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except TimeoutError:
            pass
