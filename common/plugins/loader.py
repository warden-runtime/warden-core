"""Load optional Warden plugins from the WARDEN_PLUGINS environment variable."""

from __future__ import annotations

import importlib
import logging
import os

logger = logging.getLogger(__name__)

_loaded = False


def load_plugins_from_env() -> None:
    """Import and call WARDEN_PLUGINS entrypoint once per process (no-op if unset)."""
    global _loaded
    if _loaded:
        return

    spec = os.environ.get("WARDEN_PLUGINS", "").strip()
    if not spec:
        _loaded = True
        return

    if ":" not in spec:
        msg = f"WARDEN_PLUGINS must be module.path:callable, got: {spec!r}"
        logger.error(msg)
        raise ValueError(msg)

    module_path, attr_name = spec.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
        entry = getattr(module, attr_name)
        entry()
    except Exception:
        logger.exception("Failed to load WARDEN_PLUGINS=%s", spec)
        raise

    _loaded = True
    logger.info("Loaded plugins from WARDEN_PLUGINS=%s", spec)
