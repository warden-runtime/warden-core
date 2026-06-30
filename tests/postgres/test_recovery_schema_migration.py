"""Verify greenfield 000_initial_schema.sql includes recovery columns on PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
from tortoise import Tortoise

pytestmark = [pytest.mark.postgres, pytest.mark.schema_migration]


async def _apply_sql(sql_path) -> None:
    conn = Tortoise.get_connection("default")
    sql = await asyncio.to_thread(sql_path.read_text, encoding="utf-8")
    await conn.execute_script(sql)


@pytest.fixture(autouse=True)
async def _reset_schema_after_migration_test():
    """Re-create Tortoise schema after migration test drops public."""
    yield
    if not Tortoise._inited:
        return
    conn = Tortoise.get_connection("default")
    await conn.execute_script("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    await Tortoise.generate_schemas()


@pytest.mark.asyncio
async def test_initial_schema_includes_recovery_columns(
    postgres_url: str,
    migrations_dir,
) -> None:
    """Greenfield schema must include outbox updated_at and processed_commands claim_token."""
    await Tortoise.close_connections()
    await Tortoise.init(db_url=postgres_url, modules={"models": ["common.models"]})
    conn = Tortoise.get_connection("default")
    await conn.execute_script("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")

    await _apply_sql(migrations_dir / "000_initial_schema.sql")

    outbox_cols = await conn.execute_query_dict(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'outbox_events'
        ORDER BY column_name
        """
    )
    assert {row["column_name"] for row in outbox_cols} >= {"created_at", "updated_at"}

    command_cols = await conn.execute_query_dict(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'processed_commands'
        ORDER BY column_name
        """
    )
    assert {row["column_name"] for row in command_cols} >= {"claim_token"}

    recovery_table = await conn.execute_query_dict(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'processed_operator_recoveries'
        """
    )
    assert len(recovery_table) == 1
