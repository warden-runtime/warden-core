"""Shared pagination validation for read APIs."""

from engine.api.ids import validation_http_exception
from engine.api.read_queries import DEFAULT_LIMIT, MAX_LIMIT


def validated_limit_offset(*, limit: int | None, offset: int | None) -> tuple[int, int]:
    """Return (limit, offset) or raise HTTPException 422."""
    off = 0 if offset is None else offset
    lim = DEFAULT_LIMIT if limit is None else limit
    if off < 0:
        raise validation_http_exception(
            "offset must be greater than or equal to 0.",
            loc=["query", "offset"],
        )
    if lim < 1 or lim > MAX_LIMIT:
        raise validation_http_exception(
            f"limit must be between 1 and {MAX_LIMIT} (inclusive).",
            loc=["query", "limit"],
        )
    return lim, off
