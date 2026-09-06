"""Shared catalog definition identity resolution (UUID XOR namespace/name/version)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DefinitionIdentity:
    """Exactly one of ``definition_id`` or triple fields is set."""

    definition_id: str | None = None
    namespace: str | None = None
    name: str | None = None
    version: str | None = None

    @property
    def triple(self) -> tuple[str, str, str] | None:
        if self.namespace is None or self.name is None or self.version is None:
            return None
        return (self.namespace, self.name, self.version)


class DefinitionIdentityError(ValueError):
    """Invalid combination of id / triple identity fields."""


def _strip_opt(value: str | None) -> str | None:
    raw = (value or "").strip()
    return raw or None


def _classify_identity(
    *,
    definition_id: str | None,
    namespace: str | None,
    name: str | None,
    version: str | None,
) -> tuple[bool, bool, bool]:
    has_id = definition_id is not None
    has_triple = namespace is not None and name is not None and version is not None
    partial_triple = (
        namespace is not None or name is not None or version is not None
    ) and not has_triple
    return has_id, has_triple, partial_triple


def resolve_definition_identity(
    *,
    definition_id: str | None,
    namespace: str | None,
    name: str | None,
    version: str | None,
    label: str = "definition",
) -> DefinitionIdentity:
    """Require ``id`` XOR ``namespace+name+version`` (all three)."""
    id_raw = _strip_opt(definition_id)
    ns, n, ver = _strip_opt(namespace), _strip_opt(name), _strip_opt(version)
    has_id, has_triple, partial_triple = _classify_identity(
        definition_id=id_raw, namespace=ns, name=n, version=ver
    )
    if has_id and (has_triple or partial_triple):
        raise DefinitionIdentityError(
            f"Provide either id or namespace+name+version for {label}, not both."
        )
    if partial_triple:
        raise DefinitionIdentityError(
            f"{label} by triple requires namespace, name, and version together."
        )
    if has_id:
        return DefinitionIdentity(definition_id=id_raw)
    if has_triple:
        return DefinitionIdentity(namespace=ns, name=n, version=ver)
    raise DefinitionIdentityError(f"{label} requires id or namespace+name+version.")
