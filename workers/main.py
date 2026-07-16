import asyncio
import logging

from common.config import get_settings
from common.outbox_reap_loop import run_outbox_reap_loop
from common.plugins.messaging_wire import wire_messaging_from_registry
from common.plugins.registry import get_registry
from common.telemetry import configure_logging, setup_telemetry
from common.topics import TOPIC_WORKER_COMMANDS
from workers.claim_reap import run_claim_reap_loop
from workers.db import init_db
from workers.logic import handle_worker_command

logger = logging.getLogger(__name__)


async def _await_shutdown_task(task: asyncio.Task[None]) -> None:
    """Drain a background task during shutdown without aborting worker teardown."""
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("Background task already exited during shutdown", exc_info=True)


async def main():
    """Main application entrypoint for the Worker."""
    setup_telemetry("worker-node")
    configure_logging("worker-node", level=get_settings().logging_level)

    from common.plugins.loader import load_plugins_from_env

    load_plugins_from_env()
    wire_messaging_from_registry()

    from common.prompts import validate_prompts_root_if_configured

    validate_prompts_root_if_configured()
    logger.info("Starting Worker service...")

    await init_db()
    logger.info("Worker database connection established.")

    consumer = get_registry().messaging.create_consumer(
        topic=TOPIC_WORKER_COMMANDS,
        group_id="workers_consumer_group",
        handler=handle_worker_command,
        max_in_flight=get_settings().worker_max_in_flight,
    )
    shutdown = asyncio.Event()
    consumer_task = asyncio.create_task(consumer.start())
    reap_task = asyncio.create_task(run_claim_reap_loop(shutdown))
    outbox_reap_task = asyncio.create_task(run_outbox_reap_loop(shutdown, engine_only=False))

    def handle_reaper_exit(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as e:
            logger.critical("Background worker task crashed unexpectedly: %s", e)
            shutdown.set()
            if not consumer_task.done():
                consumer_task.cancel()

    reap_task.add_done_callback(handle_reaper_exit)
    outbox_reap_task.add_done_callback(handle_reaper_exit)

    try:
        await consumer_task
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(consumer_task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            consumer_task.cancel()
            await _await_shutdown_task(consumer_task)
    except Exception as e:
        logger.exception("Worker failed to start: %s", e)
    finally:
        shutdown.set()
        reap_task.cancel()
        outbox_reap_task.cancel()
        await _await_shutdown_task(reap_task)
        await _await_shutdown_task(outbox_reap_task)
        await consumer.stop()
        if not consumer_task.done():
            consumer_task.cancel()
        await _await_shutdown_task(consumer_task)
        logger.info("Worker shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nShutting down Worker...")
