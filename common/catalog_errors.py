"""Typed catalog definition errors (HTTP 404 / 409 mapping)."""

from __future__ import annotations

from typing import Any, Literal

CatalogKind = Literal["saga", "step", "worker"]


class CatalogError(Exception):
    """Base catalog identity error with a stable machine-readable code."""

    code: str = "CATALOG_ERROR"

    def __init__(
        self,
        *,
        kind: CatalogKind,
        namespace: str,
        name: str,
        version: str,
        message: str | None = None,
    ) -> None:
        self.kind = kind
        self.namespace = namespace
        self.name = name
        self.version = version
        super().__init__(
            message or (f"{kind} definition {name!r}@{version} in namespace {namespace!r}")
        )

    def http_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "kind": self.kind,
            "namespace": self.namespace,
            "name": self.name,
            "version": self.version,
        }


class CatalogDefinitionNotFoundError(CatalogError):
    """Definition pin does not exist (HTTP 404)."""

    code = "CATALOG_DEFINITION_NOT_FOUND"

    def __init__(
        self,
        *,
        kind: CatalogKind,
        namespace: str,
        name: str,
        version: str,
        message: str | None = None,
    ) -> None:
        super().__init__(
            kind=kind,
            namespace=namespace,
            name=name,
            version=version,
            message=message
            or (
                f"{kind.capitalize()} definition not found: "
                f"namespace={namespace!r}, name={name!r}, version={version!r}"
            ),
        )


class InactiveCatalogDefinitionError(CatalogError):
    """Definition exists but is soft-disabled (HTTP 409)."""

    code = "INACTIVE_CATALOG_DEFINITION"

    def __init__(
        self,
        *,
        kind: CatalogKind,
        namespace: str,
        name: str,
        version: str,
        message: str | None = None,
    ) -> None:
        super().__init__(
            kind=kind,
            namespace=namespace,
            name=name,
            version=version,
            message=message
            or (
                f"{kind.capitalize()} definition is inactive: "
                f"namespace={namespace!r}, name={name!r}, version={version!r}"
            ),
        )
