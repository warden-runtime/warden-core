"""Shared HTTP client helpers for the warden CLI and enterprise CLI extensions."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, cast

import httpx
import typer

from common.config import get_settings

HTTP_TIMEOUT = 30.0
KEYWORD_COL = 12


def color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR", "").strip() == ""


def _style_keyword(keyword: str) -> str:
    if keyword == "REGISTER":
        return typer.style(keyword, fg=typer.colors.CYAN, bold=True)
    if keyword == "SUCCESS":
        return typer.style(keyword, fg=typer.colors.GREEN, bold=True)
    if keyword == "ERROR":
        return typer.style(keyword, fg=typer.colors.RED, bold=True)
    if keyword == "HINT":
        return typer.style(keyword, fg=typer.colors.YELLOW, dim=True)
    return typer.style(keyword, bold=True)


def _style_detail(keyword: str, detail: str) -> str:
    if keyword == "ERROR":
        return typer.style(detail, fg=typer.colors.YELLOW)
    return typer.style(detail, dim=True)


def say(keyword: str, detail: str = "", *, stream: Any = None) -> None:
    out = stream if stream is not None else sys.stdout
    use_color = color_enabled() and stream is sys.stdout
    if not detail:
        print(_style_keyword(keyword) if use_color else keyword, file=out, flush=True)
        return
    pad = max(0, KEYWORD_COL - len(keyword))
    if use_color:
        line = f"{_style_keyword(keyword)}{' ' * pad} {_style_detail(keyword, detail)}"
    else:
        line = f"{keyword.ljust(KEYWORD_COL)} {detail}"
    print(line, file=out, flush=True)


def say_err(detail: str) -> None:
    say("ERROR", detail, stream=sys.stderr)


def require_engine_url() -> str:
    raw = (get_settings().engine_url or "").strip()
    if not raw:
        say_err("ENGINE_URL is required (e.g. export ENGINE_URL=http://127.0.0.1:8000).")
        say(
            "HINT",
            "The CLI talks to the engine HTTP API only; direct DB access is not supported.",
            stream=sys.stderr,
        )
        raise typer.Exit(code=1)
    return raw.rstrip("/")


def format_api_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail, default=str)[:500]
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    body = (resp.text or "").strip().replace("\n", " ")[:400]
    return body or "(empty response body)"


async def http_request(
    method: str,
    path: str,
    *,
    content: bytes | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: list[tuple[str, str]] | None = None,
) -> httpx.Response:
    base = require_engine_url()
    url = f"{base}{path}"
    timeout = httpx.Timeout(HTTP_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(
            method,
            url,
            content=content,
            json=json_body,
            headers=headers,
            params=cast("Any", params),
        )


def handle_request_error(exc: httpx.RequestError, *, verb: str, path: str) -> None:
    msg = str(exc).strip() or type(exc).__name__
    say_err(f"{verb} {path} failed: {msg}")
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        say("HINT", "Increase timeout or check engine load.", stream=sys.stderr)
    elif "connection refused" in low or "connect" in low:
        say("HINT", "Is the engine listening? Check ENGINE_URL host and port.", stream=sys.stderr)
    elif "name or service not known" in low or "nodename" in low or "gaierror" in low:
        say("HINT", "DNS lookup failed; check ENGINE_URL hostname.", stream=sys.stderr)
