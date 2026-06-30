import asyncio
import logging
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType

from common.config import get_settings
from common.db_startup import assert_core_schema_ready
from common.plugins.tortoise_modules import model_modules_for_registry
from common.telemetry import get_tracer
from opentelemetry import trace
from tortoise import Tortoise

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)


async def init_db():
    """
    Initializes the database connection for the Worker.
    Includes retry logic to handle startup race conditions.
    """
    db_url = get_settings().db_url

    # If WORKER_MAX_IN_FLIGHT is raised materially (e.g. >8), set Tortoise pool max_size
    # to at least worker_max_in_flight so concurrent handlers do not stall on connections.

    # Security: Don't log the password
    safe_url = db_url.split("@")[-1] if "@" in db_url else "db_host"
    logger.info("Worker attempting connection to DB at ...@%s", safe_url)

    modules_config = cast(
        "dict[str, Iterable[str | ModuleType]]",
        {"models": model_modules_for_registry()},
    )
    while True:
        try:
            await Tortoise.init(db_url=db_url, modules=modules_config)

            # CRITICAL ARCHITECTURAL NOTE:
            # We do NOT run await Tortoise.generate_schemas() here.
            # 1. The Engine/Control-Plane owns the schema (DDL).
            # 2. The Worker is purely a data consumer/producer (DML).
            # 3. This prevents race conditions where two services try to create tables at once.
            await assert_core_schema_ready()

            logger.info("Worker database connection established.")
            return
        except Exception as e:
            # Trace failed attempts only: a successful first connect is not saga/work
            # traffic and would otherwise appear as a lone root trace on idle workers.
            with tracer.start_as_current_span("worker.db.connect_attempt") as span:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            logger.warning("DB Connection failed: %s. Retrying in 5s...", e)
            await asyncio.sleep(5)
