#!/usr/bin/env python3
"""Apply ordered SQL files from db/migrations and record them in schema_migrations."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from tortoise import Tortoise, connections
from tortoise.transactions import in_transaction

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(128) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _is_postgres_url(db_url: str) -> bool:
    lowered = db_url.lower()
    return lowered.startswith("postgres://") or lowered.startswith("postgresql://")


def list_migration_files() -> list[Path]:
    """Return migration SQL paths sorted by filename (000_initial_schema.sql, ...)."""
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {MIGRATIONS_DIR}")
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


async def _applied_versions() -> set[str]:
    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT version FROM schema_migrations ORDER BY version")
    return {str(row["version"]) for row in rows}


async def _ensure_tracking_table() -> None:
    conn = connections.get("default")
    await conn.execute_script(SCHEMA_MIGRATIONS_DDL)


async def _apply_one(*, version: str, sql_path: Path) -> None:
    sql = await asyncio.to_thread(sql_path.read_text, encoding="utf-8")
    async with in_transaction() as conn:
        await conn.execute_script(sql)
        await conn.execute_query(
            "INSERT INTO schema_migrations (version) VALUES ($1)",
            [version],
        )
    logger.info("Applied migration %s", version)


async def run_migrations(*, dry_run: bool = False) -> int:
    from common.config import get_settings

    db_url = get_settings().db_url
    if not _is_postgres_url(db_url):
        print(
            "migrate requires a PostgreSQL DB_URL (postgres:// or postgresql://).",
            file=sys.stderr,
        )
        return 1

    await Tortoise.init(db_url=db_url, modules={"models": ["common.models"]})
    try:
        await _ensure_tracking_table()
        applied = await _applied_versions()
        pending = [p for p in list_migration_files() if p.stem not in applied]

        if not pending:
            print("Database schema is up to date (no pending migrations).")
            return 0

        if dry_run:
            print("Pending migrations:")
            for path in pending:
                print(f"  - {path.name}")
            return 0

        for path in pending:
            await _apply_one(version=path.stem, sql_path=path)

        print(f"Applied {len(pending)} migration(s).")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Warden SQL migrations.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List pending migrations without applying them.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(asyncio.run(run_migrations(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
