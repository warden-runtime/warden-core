"""Unit tests for worker manifest schema validation."""

import pytest
from common.schemas.worker import MCPServerConfig, WorkerBlueprint
from pydantic import ValidationError


def _minimal_worker(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "worker",
        "name": "test-worker",
        "version": "0.1.0",
        "provider": "mock",
        "model_name": "demo",
        "system_prompt": "You are a test worker.",
    }
    base.update(overrides)
    return base


def test_mcp_server_config_defaults_to_streamable_http():
    cfg = MCPServerConfig(name="hosted", url="http://mcp.example/mcp")
    assert cfg.transport == "streamable_http"
    assert cfg.url == "http://mcp.example/mcp"


def test_mcp_server_config_rejects_legacy_sse_transport():
    with pytest.raises(ValidationError, match="transport 'sse' was removed"):
        MCPServerConfig(name="legacy", transport="sse", url="http://mcp.example/sse")


def test_mcp_server_config_streamable_http_requires_url():
    with pytest.raises(ValidationError, match="requires a non-empty url"):
        MCPServerConfig(name="hosted", transport="streamable_http")


def test_mcp_server_config_stdio_requires_command():
    with pytest.raises(ValidationError, match="requires a non-empty command"):
        MCPServerConfig(name="local", transport="stdio")


def test_worker_blueprint_accepts_streamable_http_tool_source():
    blueprint = WorkerBlueprint(
        **_minimal_worker(
            tool_sources=[
                {
                    "name": "company-tools",
                    "transport": "streamable_http",
                    "url": "https://mcp.internal.example.com/mcp",
                }
            ]
        )
    )
    assert blueprint.tool_sources[0].transport == "streamable_http"


def test_worker_blueprint_rejects_sse_tool_source():
    with pytest.raises(ValidationError, match="transport 'sse' was removed"):
        WorkerBlueprint(
            **_minimal_worker(
                tool_sources=[
                    {
                        "name": "legacy",
                        "transport": "sse",
                        "url": "http://mcp.example/sse",
                    }
                ]
            )
        )
