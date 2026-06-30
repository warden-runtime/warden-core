"""Resolve relative artifact paths under a configured root directory."""

from pathlib import Path


def resolve_asset_path(root: str, ref: str, *, label: str, root_var: str = "asset root") -> Path:
    """Return ``{root}/{ref}`` when the file exists; reject escapes and absolute refs."""
    if not ref or not str(ref).strip():
        raise ValueError(f"{label} path must be non-empty when set.")
    if Path(ref).is_absolute() or ".." in Path(ref).parts:
        raise ValueError(f"Invalid {label} path (no absolute paths or '..'): {ref!r}")
    base = Path(root).resolve()
    candidate = (base / ref.strip()).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise ValueError(f"{label} path escapes {root_var}: {ref!r}") from e
    if not candidate.is_file():
        raise FileNotFoundError(f"{label} file not found: {candidate}")
    return candidate


def candidate_asset_path(root: str, ref: str, *, label: str, root_var: str = "asset root") -> Path:
    """Like :func:`resolve_asset_path` but does not require the file to exist."""
    if not ref or not str(ref).strip():
        raise ValueError(f"Invalid {label} ref: {ref!r}")
    if Path(ref).is_absolute() or ".." in Path(ref).parts:
        raise ValueError(f"Invalid {label} path (no absolute paths or '..'): {ref!r}")
    base = Path(root).resolve()
    candidate = (base / ref.strip()).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise ValueError(f"{label} path escapes {root_var}: {ref!r}") from e
    return candidate
