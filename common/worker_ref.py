"""Worker definition identity helpers (namespace + name + version)."""

from typing import Any

from common.models import WorkerDefinition

WorkerIdentity = tuple[str, str]


def resolve_worker_from_compensation(
    comp_def: dict[str, Any] | None,
    *,
    forward_worker: str,
    forward_worker_version: str,
) -> WorkerIdentity:
    """Resolve compensation worker name/version, inheriting from the forward step when omitted."""
    comp = comp_def or {}
    name = str(comp.get("worker") or forward_worker)
    version = str(comp.get("worker_version") or forward_worker_version)
    return name, version


def worker_identity_label(name: str, version: str) -> str:
    """Human-readable worker definition key for logs and errors."""
    return f"{name}@{version}"


async def require_worker_definition(
    *,
    namespace: str,
    name: str,
    version: str,
) -> WorkerDefinition:
    """Load a worker definition row by full identity (namespace, name, version).

    Raises:
        ValueError: When no matching row exists.
    """
    worker_definition = await WorkerDefinition.get_or_none(
        namespace=namespace,
        name=name,
        version=version,
    )
    if worker_definition is None:
        label = worker_identity_label(name, version)
        raise ValueError(f"WorkerDefinition {label} not found in namespace {namespace!r}.")
    if worker_definition.version != version:
        label = worker_identity_label(name, version)
        raise ValueError(
            f"WorkerDefinition {label} version mismatch: row has {worker_definition.version!r}."
        )
    return worker_definition


def assert_worker_snapshot_version(
    snapshot: dict[str, Any] | None,
    *,
    expected_version: str,
) -> None:
    """Reject compensation commands whose frozen snapshot disagrees with the command version."""
    if not snapshot:
        return
    snap_version = snapshot.get("version")
    if snap_version is None:
        return
    if str(snap_version) != str(expected_version):
        raise ValueError(
            "worker_snapshot.version "
            f"{snap_version!r} does not match command worker_version {expected_version!r}."
        )
