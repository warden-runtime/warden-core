"""Process liveness for operators and the warden CLI (GET /v1/health)."""

from fastapi import APIRouter

from engine.api.schemas import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Return 200 when the API process is serving (after lifespan when using full app)."""
    return HealthResponse()
