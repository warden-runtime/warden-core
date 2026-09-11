"""Entry point for the warden-mcp server."""

from __future__ import annotations

import sys

from warden_mcp.config import get_settings
from warden_mcp.server import mcp


def main() -> None:
    settings = get_settings()
    if settings.mcp_transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
        )
        return
    # Stdio uses stdout for JSON-RPC only — never emit banners or logs there.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
