"""Load policy artifacts from disk (relative path under POLICIES_ROOT)."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from common.asset_paths import candidate_asset_path

_YAML_SUFFIXES = (".yaml", ".yml")


@dataclass(frozen=True)
class PolicyArtifact:
    """Parsed policy file: single ``cel`` expression (used for commit-step args gate)."""

    name: str
    version: str
    cel_source: str


def _default_policy_artifact_name(manifest_ref: str) -> str:
    """Stable display name from manifest ref; preserves subdirs (not bare terminal stem)."""
    ref = manifest_ref.strip()
    path = Path(ref)
    if path.suffix.lower() in _YAML_SUFFIXES:
        return str(path.with_suffix(""))
    return ref


def _resolve_policy_path_with_legacy(*, policies_root: str, manifest_ref: str) -> tuple[Path, bool]:
    """Resolve policy file: exact ``{root}/{ref}`` first, then legacy ``{root}/{ref}.yaml``."""
    ref = manifest_ref.strip()
    if not ref:
        raise ValueError(f"Invalid policy ref: {manifest_ref!r}")

    exact = candidate_asset_path(policies_root, ref, label="policy", root_var="POLICIES_ROOT")
    if exact.is_file():
        return exact, False

    if ref.lower().endswith(_YAML_SUFFIXES):
        raise FileNotFoundError(f"Policy file not found: {exact}")

    legacy = candidate_asset_path(
        policies_root, f"{ref}.yaml", label="policy", root_var="POLICIES_ROOT"
    )
    if legacy.is_file():
        return legacy, True

    raise FileNotFoundError(f"Policy file not found: {exact} or {legacy}")


def _parse_policy_file(path: Path, *, manifest_ref: str) -> PolicyArtifact:
    raw = path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Policy file must be a mapping: {path}")
    name = str(data.get("name") or _default_policy_artifact_name(manifest_ref))
    version = str(data.get("version") or "0")
    cel_raw = data.get("cel")
    if not cel_raw or not str(cel_raw).strip():
        raise ValueError(f"Policy {path} must contain a non-empty 'cel' expression.")
    return PolicyArtifact(name=name, version=version, cel_source=str(cel_raw).strip())


def _load_policy_artifact_sync(
    *, policies_root: str | None, policy_ref: str
) -> tuple[PolicyArtifact, bool]:
    if not policies_root or not str(policies_root).strip():
        raise ValueError("policies_root is not configured; set POLICIES_ROOT in the environment.")
    path, used_legacy = _resolve_policy_path_with_legacy(
        policies_root=policies_root, manifest_ref=policy_ref
    )
    return _parse_policy_file(path, manifest_ref=policy_ref), used_legacy


async def load_policy_artifact(*, policies_root: str | None, policy_name: str) -> PolicyArtifact:
    """Load policy YAML from ``POLICIES_ROOT`` using explicit path or legacy stem fallback.

    Args:
        policies_root: Base directory for policy files; if None or empty, raises.
        policy_name: Relative path under ``POLICIES_ROOT`` (e.g. ``gate.yaml`` or
            ``team-a/gate.yaml``). Legacy stem-only refs (no extension) resolve via
            ``{ref}.yaml`` when the exact path is missing.

    Returns:
        PolicyArtifact with ``cel_source`` for compilation.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If policies_root is missing, ref is unsafe, or YAML is invalid.
    """
    artifact, _used_legacy = await asyncio.to_thread(
        _load_policy_artifact_sync,
        policies_root=policies_root,
        policy_ref=policy_name,
    )
    return artifact


async def load_policy_artifact_with_meta(
    *, policies_root: str | None, policy_ref: str
) -> tuple[PolicyArtifact, bool]:
    """Load policy artifact; second value is True when legacy ``.yaml`` fallback was used."""
    return await asyncio.to_thread(
        _load_policy_artifact_sync,
        policies_root=policies_root,
        policy_ref=policy_ref,
    )
