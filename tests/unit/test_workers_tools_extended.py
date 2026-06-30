"""Unit tests for workers.tools: build_tools_for_worker, _connect_to_source, _convert_mcp_to_langchain."""

from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, MagicMock

import pytest
from common.agent_adapter import ExecutionStepError
from common.models import WorkerDefinition
from langchain_core.tools import StructuredTool
from mcp.types import Tool as McpTool
from workers.resource_runtime import READ_RESOURCE_TOOL_NAME
from workers.tools import (
    _connect_to_source,
    _convert_mcp_to_langchain,
    _env_names_from_docker_args,
    _format_mcp_exc,
    _list_resources_paginated,
    _resolve_sse_headers,
    _resolve_stdio_subprocess_env,
    _terminate_stdio_process_if_running,
    build_tools_for_worker,
)


@pytest.mark.asyncio
async def test_build_tools_for_worker_empty_sources_returns_empty_list():
    """build_tools_for_worker returns [] when worker_def.tool_sources is empty."""
    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = []

    async with AsyncExitStack() as stack:
        result = await build_tools_for_worker(
            worker_def=worker_def,
            tool_specs=[{"name": "some_tool"}],
            exit_stack=stack,
        )
    assert result == []


def test_resolve_sse_headers_returns_none_when_missing_or_empty():
    assert _resolve_sse_headers({}) is None
    assert _resolve_sse_headers({"headers": {}}) is None


def test_resolve_sse_headers_coerces_string_values():
    headers = _resolve_sse_headers({"headers": {"Authorization": "Bearer abc", "X-Custom": "1"}})
    assert headers == {"Authorization": "Bearer abc", "X-Custom": "1"}


def test_resolve_sse_headers_interpolates_env_prefix_placeholder(monkeypatch):
    monkeypatch.setenv("COMPANY_MCP_TOKEN", "secret-token")
    headers = _resolve_sse_headers(
        {"headers": {"Authorization": "Bearer ${ENV:COMPANY_MCP_TOKEN}"}}
    )
    assert headers == {"Authorization": "Bearer secret-token"}


def test_resolve_sse_headers_interpolates_bare_env_placeholder(monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", "gw-key")
    headers = _resolve_sse_headers({"headers": {"X-Api-Key": "${GATEWAY_KEY}"}})
    assert headers == {"X-Api-Key": "gw-key"}


def test_resolve_sse_headers_missing_env_substitutes_empty(monkeypatch):
    monkeypatch.delenv("MISSING_SSE_TOKEN", raising=False)
    headers = _resolve_sse_headers(
        {"headers": {"Authorization": "Bearer ${ENV:MISSING_SSE_TOKEN}"}}
    )
    assert headers == {"Authorization": "Bearer "}


@pytest.mark.asyncio
async def test_connect_to_source_sse_missing_url_returns_none():
    """_connect_to_source returns None when transport is SSE and url is missing."""
    async with AsyncExitStack() as stack:
        result = await _connect_to_source(
            source_config={"transport": "sse", "name": "test"},
            stack=stack,
        )
    assert result is None


@pytest.mark.asyncio
async def test_connect_to_source_sse_passes_headers_to_sse_client(mocker):
    """_connect_to_source forwards manifest headers to mcp sse_client."""
    mock_read = MagicMock()
    mock_write = MagicMock()
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    mock_streams_cm = MagicMock()
    mock_streams_cm.__aenter__ = AsyncMock(return_value=(mock_read, mock_write))
    mock_streams_cm.__aexit__ = AsyncMock(return_value=None)
    mock_sse_client = mocker.patch("workers.tools.sse_client", return_value=mock_streams_cm)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    mocker.patch("workers.tools.ClientSession", return_value=mock_session_cm)

    source_config = {
        "name": "hosted-mcp",
        "transport": "sse",
        "url": "http://mcp.example/sse",
        "headers": {"Authorization": "Bearer test-token"},
    }

    async with AsyncExitStack() as stack:
        await _connect_to_source(source_config, stack)

    mock_sse_client.assert_called_once_with(
        "http://mcp.example/sse",
        headers={"Authorization": "Bearer test-token"},
    )


@pytest.mark.asyncio
async def test_connect_to_source_unknown_transport_returns_none():
    """_connect_to_source returns None for unsupported transport."""
    async with AsyncExitStack() as stack:
        result = await _connect_to_source(
            source_config={"transport": "grpc", "name": "test"},
            stack=stack,
        )
    assert result is None


@pytest.mark.asyncio
async def test_convert_mcp_to_langchain_returns_structured_tool():
    """_convert_mcp_to_langchain returns a StructuredTool with correct name and schema."""
    mcp_tool = McpTool(
        name="echo",
        description="Echo input",
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["message"],
        },
    )
    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))

    tool = _convert_mcp_to_langchain(mcp_tool, mock_session, step_spec=None)

    assert isinstance(tool, StructuredTool)
    assert tool.name == "echo"
    assert tool.description == "Echo input"
    await tool.ainvoke({"message": "hi", "count": 2})
    mock_session.call_tool.assert_called_once_with("echo", arguments={"message": "hi", "count": 2})


@pytest.mark.asyncio
async def test_convert_mcp_to_langchain_maps_array_and_object_types():
    """_convert_mcp_to_langchain preserves array/object JSON schema types."""
    mcp_tool = McpTool(
        name="complex",
        description="Complex args",
        inputSchema={
            "type": "object",
            "properties": {
                "tags": {"type": "array"},
                "meta": {"type": "object"},
            },
            "required": ["tags"],
        },
    )
    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))

    tool = _convert_mcp_to_langchain(mcp_tool, mock_session, step_spec=None)
    await tool.ainvoke({"tags": ["a"], "meta": {"k": "v"}})

    mock_session.call_tool.assert_called_once_with(
        "complex",
        arguments={"tags": ["a"], "meta": {"k": "v"}},
    )


@pytest.mark.asyncio
async def test_convert_mcp_to_langchain_ainvoke_calls_session_and_returns_text():
    """_convert_mcp_to_langchain tool ainvoke calls session.call_tool and returns concatenated text."""
    from mcp.types import TextContent

    mcp_tool = McpTool(
        name="greet",
        description="Greet",
        inputSchema={"type": "object", "properties": {}, "required": []},
    )
    mock_session = MagicMock()
    mock_session.call_tool = AsyncMock(
        return_value=MagicMock(content=[TextContent(type="text", text="Hello")])
    )

    tool = _convert_mcp_to_langchain(mcp_tool, mock_session, step_spec=None)
    result = await tool.ainvoke({})

    assert result == "Hello"
    mock_session.call_tool.assert_called_once_with("greet", arguments={})


@pytest.mark.asyncio
async def test_build_tools_for_worker_with_mocked_connect_and_list_tools(mocker):
    """build_tools_for_worker returns tools when _connect_to_source and list_tools are mocked."""
    mock_session = MagicMock()
    mock_session.list_tools = AsyncMock(
        return_value=MagicMock(
            tools=[
                McpTool(name="allowed_tool", description="A tool", inputSchema={}),
            ]
        )
    )

    async def _fake_connect(source_config, stack):
        return mock_session

    mocker.patch(
        "workers.tools._connect_to_source",
        new_callable=AsyncMock,
        side_effect=_fake_connect,
    )

    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = [{"name": "mcp1", "transport": "sse", "url": "http://localhost"}]

    async with AsyncExitStack() as stack:
        result = await build_tools_for_worker(
            worker_def=worker_def,
            tool_specs=[{"name": "allowed_tool"}],
            exit_stack=stack,
        )

    assert len(result) == 1
    assert isinstance(result[0], StructuredTool)
    assert result[0].name == "allowed_tool"


def test_format_mcp_exc_flattens_exception_group():
    """_format_mcp_exc stringifies nested ExceptionGroup (e.g. MCP TaskGroup)."""
    eg = ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        (ConnectionError("Connection refused"),),
    )
    text = _format_mcp_exc(eg)
    assert "ConnectionError" in text
    assert "Connection refused" in text


@pytest.mark.asyncio
async def test_build_tools_for_worker_raises_mcp_unavailable_when_all_sources_fail(mocker):
    """When every MCP source fails, raise ExecutionStepError with code MCP_UNAVAILABLE."""

    async def _boom(_source_config, _stack):
        raise OSError("no route to host")

    mocker.patch("workers.tools._connect_to_source", new_callable=AsyncMock, side_effect=_boom)

    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = [
        {"name": "test-mcp", "transport": "sse", "url": "http://test-mcp:8765/sse"},
    ]

    with pytest.raises(ExecutionStepError) as exc_info:
        async with AsyncExitStack() as stack:
            await build_tools_for_worker(
                worker_def=worker_def,
                tool_specs=[{"name": "get_claim_facts"}],
                exit_stack=stack,
            )

    details = exc_info.value.error_details
    assert details.get("code") == "MCP_UNAVAILABLE"
    assert details.get("missing_tools") == ["get_claim_facts"]
    assert len(details.get("source_failures") or []) == 1


@pytest.mark.asyncio
async def test_build_tools_for_worker_second_source_succeeds_if_first_fails(mocker):
    """A failing MCP source is skipped; a later source can still supply the tool."""
    mock_session_ok = MagicMock()
    mock_session_ok.list_tools = AsyncMock(
        return_value=MagicMock(
            tools=[McpTool(name="get_claim_facts", description="x", inputSchema={})]
        )
    )
    call_count = {"n": 0}

    async def _connect_side_effect(source_config, stack):
        call_count["n"] += 1
        if source_config.get("name") == "bad-mcp":
            raise ConnectionError("refused")
        return mock_session_ok

    mocker.patch(
        "workers.tools._connect_to_source",
        new_callable=AsyncMock,
        side_effect=_connect_side_effect,
    )

    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = [
        {"name": "bad-mcp", "transport": "sse", "url": "http://bad:1/sse"},
        {"name": "good-mcp", "transport": "sse", "url": "http://good:2/sse"},
    ]

    async with AsyncExitStack() as stack:
        result = await build_tools_for_worker(
            worker_def=worker_def,
            tool_specs=[{"name": "get_claim_facts"}],
            exit_stack=stack,
        )

    assert len(result) == 1
    assert result[0].name == "get_claim_facts"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_connect_to_source_stdio_passes_cwd_and_env_to_StdioServerParameters(mocker):
    """_connect_to_source passes cwd and env from source_config to StdioServerParameters."""
    mock_read = MagicMock()
    mock_write = MagicMock()
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.terminate = MagicMock()
    mock_process.wait = AsyncMock(return_value=0)

    mock_streams_cm = MagicMock()
    mock_streams_cm.__aenter__ = AsyncMock(return_value=(mock_read, mock_write, mock_process))
    mock_streams_cm.__aexit__ = AsyncMock(return_value=None)
    mock_tracked_stdio = MagicMock(return_value=mock_streams_cm)

    # ClientSession(read, write) returns an async context manager that yields session
    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    mock_client_session = MagicMock(return_value=mock_session_cm)

    mocker.patch("workers.tools._tracked_stdio_client", mock_tracked_stdio)
    mocker.patch("workers.tools.ClientSession", mock_client_session)
    mock_params_class = mocker.patch("workers.tools.StdioServerParameters")
    terminate_spy = mocker.patch(
        "workers.tools._terminate_stdio_process_if_running",
        new=AsyncMock(),
    )

    source_config = {
        "name": "stdio-server",
        "transport": "stdio",
        "command": "node",
        "args": ["/app/workspace-server/dist/index.js"],
        "cwd": "/app/workspace",
        "env": {"WORKSPACE_CLIENT_ID": "test-id"},
    }

    async with AsyncExitStack() as stack:
        await _connect_to_source(source_config, stack)

    mock_params_class.assert_called_once()
    params = mock_params_class.call_args.kwargs
    assert params["command"] == "node"
    assert params["args"] == ["/app/workspace-server/dist/index.js"]
    assert params["cwd"] == "/app/workspace"
    assert params["env"]["WORKSPACE_CLIENT_ID"] == "test-id"
    terminate_spy.assert_awaited_once_with(mock_process)


@pytest.mark.asyncio
async def test_terminate_stdio_process_if_running_terminates_live_process():
    process = MagicMock()
    process.returncode = None
    process.terminate = MagicMock()
    process.wait = AsyncMock(return_value=0)
    await _terminate_stdio_process_if_running(process)
    process.terminate.assert_called_once()
    process.wait.assert_awaited_once()


def test_env_names_from_docker_args_collects_bare_e_flags():
    args = ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "-e", "FOO=bar", "image"]
    assert _env_names_from_docker_args(args) == ["GITHUB_PERSONAL_ACCESS_TOKEN"]


def test_resolve_stdio_subprocess_env_inherits_docker_e_from_os_environ(mocker):
    mocker.patch.dict(
        "os.environ",
        {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test", "PATH": "/usr/bin"},
        clear=False,
    )
    env = _resolve_stdio_subprocess_env(
        {
            "args": [
                "run",
                "-e",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "ghcr.io/github/github-mcp-server",
            ],
        }
    )
    assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test"
    assert "PATH" in env


def test_missing_stdio_env_vars_detects_unset_pat(mocker):
    mocker.patch.dict("os.environ", {}, clear=True)
    source = {
        "args": ["run", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
        "env_inherit": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    }
    from workers.tools import _missing_stdio_env_vars

    assert _missing_stdio_env_vars(source) == ["GITHUB_PERSONAL_ACCESS_TOKEN"]


@pytest.mark.asyncio
async def test_connect_stdio_source_fails_fast_when_pat_missing(mocker):
    mocker.patch.dict("os.environ", {}, clear=True)
    from workers.tools import _connect_stdio_source

    source = {
        "command": "docker",
        "args": ["run", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
        "env_inherit": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    }
    with pytest.raises(RuntimeError, match="GITHUB_PERSONAL_ACCESS_TOKEN"):
        async with AsyncExitStack() as stack:
            await _connect_stdio_source(source_config=source, stack=stack)


@pytest.mark.asyncio
async def test_build_tools_for_worker_adds_read_resource_when_resource_specs_set(mocker):
    mock_session = MagicMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
    mock_session.list_resources = AsyncMock(return_value=MagicMock(resources=[]))

    async def _fake_connect(source_config, stack):
        return mock_session

    mocker.patch(
        "workers.tools._connect_to_source",
        new_callable=AsyncMock,
        side_effect=_fake_connect,
    )

    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = [{"name": "mcp1", "transport": "sse", "url": "http://localhost"}]

    async with AsyncExitStack() as stack:
        result = await build_tools_for_worker(
            worker_def=worker_def,
            tool_specs=[],
            exit_stack=stack,
            resource_specs=[{"uri": "file:///policies/fraud-v3.md"}],
        )

    assert len(result) == 1
    assert result[0].name == READ_RESOURCE_TOOL_NAME


@pytest.mark.asyncio
async def test_build_tools_for_worker_read_resource_enforces_allowlist(mocker):
    from mcp.types import TextResourceContents

    mock_session = MagicMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
    mock_session.list_resources = AsyncMock(return_value=MagicMock(resources=[]))
    mock_session.read_resource = AsyncMock(
        return_value=MagicMock(
            contents=[TextResourceContents(uri="file:///policies/fraud-v3.md", text="policy")]
        )
    )

    mocker.patch(
        "workers.tools._connect_to_source",
        new_callable=AsyncMock,
        return_value=mock_session,
    )

    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = [{"name": "mcp1", "transport": "sse", "url": "http://localhost"}]

    async with AsyncExitStack() as stack:
        tools = await build_tools_for_worker(
            worker_def=worker_def,
            tool_specs=[],
            exit_stack=stack,
            resource_specs=[{"uri": "file:///policies/fraud-v3.md"}],
        )

    read_tool = tools[0]
    result = await read_tool.ainvoke({"uri": "file:///policies/fraud-v3.md"})
    assert result == "policy"

    with pytest.raises(ExecutionStepError) as exc_info:
        await read_tool.ainvoke({"uri": "file:///policies/other.md"})
    assert exc_info.value.error_details.get("code") == "RESOURCE_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_build_tools_for_worker_read_resource_blocks_variable_mismatch_before_network(mocker):
    mock_session = MagicMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
    mock_session.list_resources = AsyncMock(return_value=MagicMock(resources=[]))
    mock_session.read_resource = AsyncMock(return_value=MagicMock(contents=[]))
    mocker.patch(
        "workers.tools._connect_to_source",
        new_callable=AsyncMock,
        return_value=mock_session,
    )

    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = [{"name": "mcp1", "transport": "sse", "url": "http://localhost"}]
    context = {"saga_vars": {"tenant_id": "tenant-a"}}
    async with AsyncExitStack() as stack:
        tools = await build_tools_for_worker(
            worker_def=worker_def,
            tool_specs=[],
            exit_stack=stack,
            context=context,
            resource_specs=[{"uri": "file:///tenants/{tenant_id}/policy.md"}],
        )
    with pytest.raises(ExecutionStepError) as exc_info:
        await tools[0].ainvoke({"uri": "file:///tenants/tenant-b/policy.md"})
    assert exc_info.value.error_details.get("code") == "RESOURCE_URI_VAR_MISMATCH"
    mock_session.read_resource.assert_not_called()


@pytest.mark.asyncio
async def test_list_resources_paginated_consumes_cursor_and_respects_max_pages():
    page1 = MagicMock(resources=[MagicMock(uri="file:///a")], nextCursor="n1")
    page2 = MagicMock(resources=[MagicMock(uri="file:///b")], nextCursor="n2")
    page3 = MagicMock(resources=[MagicMock(uri="file:///c")], nextCursor=None)
    session = MagicMock()
    session.list_resources = AsyncMock(side_effect=[page1, page2, page3])
    resources = await _list_resources_paginated(session, timeout_s=1, max_pages=2, max_items=10)
    assert resources == ["file:///a", "file:///b"]
    assert session.list_resources.await_count == 2


@pytest.mark.asyncio
async def test_build_tools_for_worker_requires_sources_when_resources_configured():
    worker_def = MagicMock(spec=WorkerDefinition)
    worker_def.tool_sources = []

    with pytest.raises(ExecutionStepError) as exc_info:
        async with AsyncExitStack() as stack:
            await build_tools_for_worker(
                worker_def=worker_def,
                tool_specs=[],
                exit_stack=stack,
                resource_specs=[{"uri": "file:///policies/fraud-v3.md"}],
            )
    assert exc_info.value.error_details.get("code") == "MCP_SOURCES_REQUIRED"
