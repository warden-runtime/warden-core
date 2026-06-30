"""Minimal WARDEN_PLUGINS entrypoint for loader smoke tests."""

_called = False


def install() -> None:
    global _called
    _called = True
