"""Shared helpers for converting Pydantic ValidationError into deploy-time ValueError."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import ValidationError


def manifest_validation_error(exc: ValidationError) -> ValueError:
    """Convert blueprint ValidationError into a deploy-time ValueError for API/CLI."""
    messages: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(segment) for segment in err.get("loc", ()))
        msg = str(err.get("msg", "validation error"))
        if msg.startswith("Value error, "):
            msg = msg.removeprefix("Value error, ")
        messages.append(f"{loc}: {msg}" if loc else msg)
    if not messages:
        return ValueError("Manifest validation failed.")
    return ValueError(messages[0] if len(messages) == 1 else "; ".join(messages))
