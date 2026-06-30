"""
Stdio MCP fixture for the mock LLM + MCP demo.

NEVER write to stdout except via the MCP SDK transport — stdout is the JSON-RPC pipe.
Log diagnostics to stderr only.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

server = Server("mock-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="echo",
            description="Echo a message back to the caller.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    if name != "echo":
        raise ValueError(f"Unknown tool: {name!r}")
    args = arguments or {}
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("echo requires a non-empty string 'message'")
    text = f"echo: {message}"
    logger.info("echo tool called message=%r", message)
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    init_options = InitializationOptions(
        server_name="mock-mcp",
        server_version="0.1.0",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    asyncio.run(main())
