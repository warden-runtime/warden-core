"""Helpers for slim worker-command allowlists on the outbox wire."""


def slim_tool_specs(tool_specs: list[dict] | None) -> list[dict[str, str]]:
    """Emit minimal tool allowlist entries (name only) for outbox payloads."""
    if not tool_specs:
        return []
    slimmed: list[dict[str, str]] = []
    for spec in tool_specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if name:
            slimmed.append({"name": str(name)})
    return slimmed
