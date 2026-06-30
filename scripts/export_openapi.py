#!/usr/bin/env python3
"""Export the Engine FastAPI OpenAPI schema for Docusaurus API reference docs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT = _REPO_ROOT / "website" / "openapi" / "engine.openapi.json"


def _project_version() -> str:
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version"):
            _, _, value = line.partition("=")
            return value.strip().strip('"').strip("'")
    return "0.0.0"


def build_openapi() -> dict:
    from engine.api.api import app

    schema = app.openapi()
    schema.setdefault("info", {})
    schema["info"]["title"] = "API Reference"
    schema["info"]["description"] = (
        "Control-plane HTTP API for deploying manifests, starting sagas, "
        "HITL review, and operator recovery. OSS surface under /v1."
    )
    schema["info"]["version"] = _project_version()
    schema["servers"] = [{"url": "http://127.0.0.1:8000", "description": "Local engine"}]
    return schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Export engine OpenAPI JSON.")
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"Output path (default: {_DEFAULT_OUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if committed file differs from current schema.",
    )
    args = parser.parse_args()

    schema = build_openapi()
    rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.out.is_file():
            print(f"Missing OpenAPI file: {args.out}", file=sys.stderr)
            return 1
        existing = args.out.read_text(encoding="utf-8")
        if existing != rendered:
            print(
                f"OpenAPI drift: regenerate with `uv run python scripts/export_openapi.py` "
                f"and commit {args.out.relative_to(_REPO_ROOT)}",
                file=sys.stderr,
            )
            return 1
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
