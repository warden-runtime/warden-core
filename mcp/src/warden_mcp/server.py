"""FastMCP server instance and tool registration."""

from contextlib import asynccontextmanager

from fastmcp import FastMCP

from warden_mcp.client import close_shared_http_client, open_shared_http_client
from warden_mcp.tools import register_tools


@asynccontextmanager
async def _engine_client_lifespan(_server):
    await open_shared_http_client()
    try:
        yield
    finally:
        await close_shared_http_client()


mcp = FastMCP(
    "warden",
    instructions=(
        "Control the Warden durable saga runtime via its engine HTTP API. "
        "Catalog deploy order is workers → steps → sagas (kind: step is a first-class "
        "capability; sagas compose with use:/version). Call warden_ready before "
        "deploy/start. Mutating saga operations enqueue work asynchronously (HTTP 202); "
        "poll saga/runtime-step endpoints until terminal status or AWAITING_HUMAN. "
        "warden_list_step_definitions is the catalog; warden_list_saga_steps is runtime."
    ),
    lifespan=_engine_client_lifespan,
)

register_tools(mcp)
