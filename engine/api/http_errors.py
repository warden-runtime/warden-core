"""Shared HTTP mapping for catalog and structured control-plane errors."""

from __future__ import annotations

from typing import Any

from common.catalog_errors import (
    CatalogDefinitionNotFoundError,
    CatalogError,
    CatalogKind,
    InactiveCatalogDefinitionError,
)
from fastapi import HTTPException


def structured_detail(*, code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Machine-readable ``detail`` body: ``{code, message, ...}``."""
    return {"code": code, "message": message, **extra}


def http_exception_for_catalog(exc: CatalogError) -> HTTPException:
    """Map catalog identity errors to 404 (missing) or 409 (inactive)."""
    if isinstance(exc, CatalogDefinitionNotFoundError):
        status = 404
    elif isinstance(exc, InactiveCatalogDefinitionError):
        status = 409
    else:
        status = 400
    return HTTPException(status_code=status, detail=exc.http_detail())


def definition_not_found_http(
    *,
    kind: CatalogKind,
    definition_id: str | None = None,
    namespace: str | None = None,
    name: str | None = None,
    version: str | None = None,
) -> HTTPException:
    """404 with ``CATALOG_DEFINITION_NOT_FOUND`` for UUID or triple lookups."""
    if definition_id is not None:
        return HTTPException(
            status_code=404,
            detail=structured_detail(
                code="CATALOG_DEFINITION_NOT_FOUND",
                message=f"{kind.capitalize()} definition not found for id={definition_id!r}.",
                kind=kind,
                id=definition_id,
            ),
        )
    if namespace is None or name is None or version is None:
        raise ValueError("definition_not_found_http requires definition_id or full triple")
    return http_exception_for_catalog(
        CatalogDefinitionNotFoundError(
            kind=kind,
            namespace=namespace,
            name=name,
            version=version,
        )
    )
