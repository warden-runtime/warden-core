import asyncio
import logging

from common.config import get_settings
from common.db_startup import assert_core_schema_ready
from common.plugins.tortoise_modules import model_modules_for_registry
from tortoise import Tortoise

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Initialize Tortoise and verify migrated schema with a retry loop.

    Uses DB URL from app config. Retries up to 15 times with 5s delay.
    Loads registry model modules and asserts schema is already migrated.

    Raises:
        Exception: Re-raises the last connection error after max retries.
    """
    db_url = get_settings().db_url

    max_retries = 15
    retry_interval = 5

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "Attempting to connect to database (Attempt %s/%s)...",
                attempt,
                max_retries,
            )

            await Tortoise.init(db_url=db_url, modules={"models": model_modules_for_registry()})

            await assert_core_schema_ready()

            logger.info("Database connection established.")
            return

        except Exception as e:
            if attempt == max_retries:
                logger.critical(
                    "Failed to connect to DB after %s attempts. Exiting.",
                    max_retries,
                )
                raise e

            logger.warning("Database connection failed: %s", e)
            logger.info("Retrying in %s seconds...", retry_interval)
            await asyncio.sleep(retry_interval)
