"""Integration smoke test for workers.fixtures.mock_mcp_server stdio transport."""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_mock_mcp_server_echo_tool_over_stdio():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "workers.fixtures.mock_mcp_server"],
        env={"PYTHONPATH": "."},
    )
    async with AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "echo" in names
        result = await session.call_tool("echo", arguments={"message": "hello demo"})
        texts = [block.text for block in result.content if block.type == "text"]
        assert texts == ["echo: hello demo"]
