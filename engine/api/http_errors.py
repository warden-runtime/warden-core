"""Shared HTTP mapping for catalog definition errors."""

from __future__ import annotations

from common.catalog_errors import (
    CatalogDefinitionNotFoundError,
    CatalogError,
    InactiveCatalogDefinitionError,
)
from fastapi import HTTPException


def http_exception_for_catalog(exc: CatalogError) -> HTTPException:
    """Map catalog identity errors to 404 (missing) or 409 (inactive)."""
    if isinstance(exc, CatalogDefinitionNotFoundError):
        status = 404
    elif isinstance(exc, InactiveCatalogDefinitionError):
        status = 409
    else:
        status = 400
    return HTTPException(status_code=status, detail=exc.http_detail())
