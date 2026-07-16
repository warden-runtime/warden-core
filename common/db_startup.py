"""Database startup checks for migration-only runtime paths."""

from __future__ import annotations

from tortoise import connections

REQUIRED_CORE_TABLES: tuple[str, ...] = (
    "saga_definitions",
    "saga_instances",
    "saga_step_instances",
    "outbox_events",
    "processed_commands",
)

REQUIRED_CORE_COLUMNS: dict[str, tuple[str, ...]] = {
    # Baseline schema column; catches partial migration state early.
    "saga_step_instances": (
        "max_turns",
        "execution_timing",
        "pending_engine_timing",
        "execution_usage",
    ),
}


async def _table_exists(*, table: str) -> bool:
    conn = connections.get("default")
    if conn.capabilities.dialect == "postgres":
        rows = await conn.execute_query_dict(
            "SELECT to_regclass($1) AS regclass",
            [table],
        )
        return bool(rows and rows[0].get("regclass"))
    rows = await conn.execute_query_dict(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        [table],
    )
    return bool(rows)


async def _missing_columns(*, table: str, columns: tuple[str, ...]) -> list[str]:
    conn = connections.get("default")
    if conn.capabilities.dialect == "postgres":
        rows = await conn.execute_query_dict(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = $1
            """,
            [table],
        )
        present = {str(r.get("column_name")) for r in rows}
    else:
        rows = await conn.execute_query_dict(f"PRAGMA table_info({table})")
        present = {str(r.get("name")) for r in rows}
    return [c for c in columns if c not in present]


async def assert_core_schema_ready() -> None:
    """Fail fast when runtime tables/columns are missing.

    Runtime services must not create schema at startup; migrations are required.
    """
    missing_tables: list[str] = []
    for table in REQUIRED_CORE_TABLES:
        if not await _table_exists(table=table):
            missing_tables.append(table)
    if missing_tables:
        raise RuntimeError(
            "Database schema is not initialized (missing tables: "
            f"{', '.join(sorted(missing_tables))}). "
            "Run `make up` (migrate runs via Compose), or `make migrate` / "
            "`make migrate-compose` when only Postgres is up."
        )

    missing_details: dict[str, list[str]] = {}
    for table, cols in REQUIRED_CORE_COLUMNS.items():
        missing = await _missing_columns(table=table, columns=cols)
        if missing:
            missing_details[table] = missing
    if missing_details:
        detail = ", ".join(f"{t}[{', '.join(cols)}]" for t, cols in missing_details.items())
        raise RuntimeError(
            "Database schema is out of date (missing columns: "
            f"{detail}). Run `make migrate` before startup."
        )
