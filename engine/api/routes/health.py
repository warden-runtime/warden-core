"""Process liveness for operators and the warden CLI (GET /v1/health)."""

from fastapi import APIRouter, HTTPException
from tortoise import Tortoise

from engine.api.schemas import HealthResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Return 200 when the API process is serving (after lifespan when using full app)."""
    return HealthResponse()


@router.get("/ready", response_model=ReadinessResponse)
async def get_readiness() -> ReadinessResponse:
    """Return 200 when the API can reach Postgres; 503 otherwise."""
    try:
        conn = Tortoise.get_connection("default")
        await conn.execute_query("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database not ready: {exc}") from exc
    return ReadinessResponse(status="ready", database="ok")
