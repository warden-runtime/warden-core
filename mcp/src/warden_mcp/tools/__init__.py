"""MCP tool registration."""

from fastmcp import FastMCP

from warden_mcp.tools import definitions, health, hitl, manifests, recovery, sagas


def register_tools(mcp: FastMCP) -> None:
    health.register(mcp)
    manifests.register(mcp)
    definitions.register(mcp)
    sagas.register(mcp)
    hitl.register(mcp)
    recovery.register(mcp)
