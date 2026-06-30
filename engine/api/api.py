"""FastAPI app for the engine: saga start and future control-plane endpoints."""

import asyncio
import logging
from contextlib import asynccontextmanager

from common.config import get_settings
from common.db_startup import assert_core_schema_ready
from common.plugins.registry import get_registry
from common.plugins.tortoise_modules import model_modules_for_registry
from common.telemetry import instrument_fastapi_app
from fastapi import FastAPI
from tortoise.contrib.fastapi import RegisterTortoise

from engine.api.routes.definitions import router as definitions_router
from engine.api.routes.health import router as health_router
from engine.api.routes.human_gate import router as human_gate_router
from engine.api.routes.manifests import router as manifests_router
from engine.api.routes.recovery import router as recovery_router
from engine.api.routes.sagas import router as sagas_router

logger = logging.getLogger(__name__)

# Set after Uvicorn binds the HTTP listener; main() waits before starting the consumer.
api_listening = asyncio.Event()


def notify_api_listening(host: str, port: int) -> None:
    """Record that the HTTP server socket is bound (called from engine.main after Uvicorn startup)."""
    logger.info("Engine API listening on %s:%s", host, port)
    api_listening.set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Register Tortoise with app and verify migrated schema.

    Uvicorn runs ASGI lifespan before binding the listen socket; consumer startup must
    wait on ``api_listening`` (set after bind), not on lifespan entry alone.

    Args:
        app: FastAPI application instance.
    """
    db_url = get_settings().db_url
    async with RegisterTortoise(
        app=app,
        db_url=db_url,
        modules={"models": model_modules_for_registry()},
        generate_schemas=False,
        _enable_global_fallback=True,
    ):
        await assert_core_schema_ready()
        await get_registry().http.mount(app)
        yield
    logger.info("Engine API shutdown.")


app = FastAPI(
    title="Engine API",
    description="Control-plane API for starting and managing sagas and deploying manifests.",
    lifespan=lifespan,
)
# Versioned surface: /v1/sagas/..., /v1/manifests/..., /v1/health
app.include_router(health_router, prefix="/v1")
app.include_router(sagas_router, prefix="/v1")
app.include_router(definitions_router, prefix="/v1")
app.include_router(human_gate_router, prefix="/v1")
app.include_router(recovery_router, prefix="/v1")
app.include_router(manifests_router, prefix="/v1")
# Inbound trace context must be active before plugin routers handle requests.
instrument_fastapi_app(app)
