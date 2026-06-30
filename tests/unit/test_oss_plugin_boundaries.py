"""OSS HTTP/CLI plugin boundaries: NoOp registry must not expose audit routes or CLI."""

from __future__ import annotations

import pytest
import typer
from common.plugins import reset_registry
from common.plugins.noop import NoOpCliExtensionRegistry
from common.plugins.registry import get_registry
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _isolated_registry():
    reset_registry()
    yield
    reset_registry()


def _typer_group_names(root: typer.Typer) -> set[str]:
    return {g.name for g in root.registered_groups if g.name}


def test_oss_cli_has_no_audit_subcommand():
    root = typer.Typer()
    get_registry().cli.register(root)
    assert "audit" not in _typer_group_names(root)
    assert isinstance(get_registry().cli, NoOpCliExtensionRegistry)


@pytest.mark.asyncio
async def test_oss_fastapi_has_no_audit_routes():
    from engine.api.api import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/audit-events")
    assert resp.status_code == 404
