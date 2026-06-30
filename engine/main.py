import asyncio
import logging

from common.config import get_settings
from common.plugins.messaging_wire import wire_messaging_from_registry
from common.plugins.registry import get_registry
from common.telemetry import configure_logging, setup_telemetry
from common.topics import TOPIC_ORCHESTRATOR_EVENTS

logger = logging.getLogger(__name__)


async def _teardown_engine(
    *,
    consumer,
    api_task: asyncio.Task[None],
    consumer_task: asyncio.Task[None],
) -> None:
    await consumer.stop()
    for task in (api_task, consumer_task):
        if not task.done():
            task.cancel()
    try:
        await asyncio.gather(
            asyncio.wait_for(api_task, timeout=5.0),
            asyncio.wait_for(consumer_task, timeout=5.0),
        )
    except (TimeoutError, asyncio.CancelledError):
        pass


async def run_api_server():
    """Run the FastAPI app (POST /v1/sagas/start) in the same process as the consumer."""
    from uvicorn import Config, Server

    from engine.api.api import app, notify_api_listening

    class EngineServer(Server):
        """Signal listen readiness only after Uvicorn binds the HTTP socket."""

        async def startup(self, sockets=None) -> None:
            await super().startup(sockets=sockets)
            if self.started:
                notify_api_listening(self.config.host, self.config.port)

    s = get_settings()
    config = Config(
        app=app,
        host=s.engine_api_host,
        port=s.engine_api_port,
    )
    await EngineServer(config).serve()


async def main():
    """Main application entrypoint: API server + outbox consumer."""
    from common.plugins.loader import load_plugins_from_env

    load_plugins_from_env()
    wire_messaging_from_registry()

    setup_telemetry("engine-node")
    configure_logging("engine-node")

    from common.prompts import validate_prompts_root_if_configured

    validate_prompts_root_if_configured()
    logger.info("Starting Engine...")

    from common.outbox_reap_loop import run_outbox_reap_loop

    from engine.api.api import api_listening
    from engine.logic import process_saga_event

    api_task = asyncio.create_task(run_api_server())
    await api_listening.wait()

    consumer = get_registry().messaging.create_consumer(
        topic=TOPIC_ORCHESTRATOR_EVENTS,
        group_id="engine_consumer_group",
        handler=process_saga_event,
    )
    consumer_task = asyncio.create_task(consumer.start())
    shutdown = asyncio.Event()
    outbox_reap_task = asyncio.create_task(run_outbox_reap_loop(shutdown, engine_only=True))
    try:
        await asyncio.gather(api_task, consumer_task)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("Engine failed: %s", e)
    finally:
        shutdown.set()
        outbox_reap_task.cancel()
        try:
            await outbox_reap_task
        except asyncio.CancelledError:
            pass
        await _teardown_engine(
            consumer=consumer,
            api_task=api_task,
            consumer_task=consumer_task,
        )
        logger.info("Engine shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nShutting down Engine...")
