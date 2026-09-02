"""
Stdio MCP fixture for the mock LLM + MCP demo.

NEVER write to stdout except via the MCP SDK transport — stdout is the JSON-RPC pipe.
Log diagnostics to stderr only.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server import InitializationOptions, NotificationOptions, Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def handle_list_tools(
    ctx: ServerRequestContext, params: PaginatedRequestParams | None
) -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(
                name="echo",
                description="Echo a message back to the caller.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                    },
                    "required": ["message"],
                },
            )
        ]
    )


async def handle_call_tool(
    ctx: ServerRequestContext, params: CallToolRequestParams
) -> CallToolResult:
    if params.name != "echo":
        raise ValueError(f"Unknown tool: {params.name!r}")
    args = params.arguments or {}
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("echo requires a non-empty string 'message'")
    text = f"echo: {message}"
    logger.info("echo tool called message=%r", message)
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=False)


server = Server("mock-mcp", on_list_tools=handle_list_tools, on_call_tool=handle_call_tool)


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
