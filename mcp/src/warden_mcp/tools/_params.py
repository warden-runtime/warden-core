"""Shared query parameter serialization for engine list APIs."""

from __future__ import annotations


def query_params(**kwargs: str | int | bool | None) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, bool):
            params.append((key, "true" if value else "false"))
        else:
            params.append((key, str(value)))
    return params
