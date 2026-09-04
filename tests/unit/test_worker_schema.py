"""Unit tests for worker manifest schema validation."""

from typing import Any

import pytest
from common.schemas.worker import MCPServerConfig, WorkerBlueprint
from pydantic import ValidationError
from tests.factories import worker_definition_body


def _minimal_worker(**overrides: Any) -> dict[str, Any]:
    return worker_definition_body(
        version="0.1.0",
        provider="mock",
        model_name="demo",
        system_prompt="You are a test worker.",
        **overrides,
    )


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


def test_parse_worker_blueprint_round_trips_body():
    from common.schemas.worker import parse_worker_blueprint

    raw = _minimal_worker(temperature=0.3, description="note")
    blueprint = parse_worker_blueprint(raw)
    assert blueprint.temperature == 0.3
    assert blueprint.description == "note"
    dumped = blueprint.model_dump(by_alias=True, exclude_none=True)
    assert dumped["provider"] == "mock"
    assert dumped["temperature"] == 0.3
    again = parse_worker_blueprint(dumped)
    assert again.model_name == "demo"
