from __future__ import annotations

from types import SimpleNamespace

import pytest
from common.db_startup import REQUIRED_CORE_COLUMNS, REQUIRED_CORE_TABLES, assert_core_schema_ready
from tortoise import Tortoise


class _FakeConn:
    def __init__(
        self, *, dialect: str, existing_tables: set[str], columns: dict[str, set[str]]
    ) -> None:
        self.capabilities = SimpleNamespace(dialect=dialect)
        self._tables = existing_tables
        self._columns = columns

    async def execute_query_dict(self, query: str, params=None):  # type: ignore[no-untyped-def]
        if "to_regclass" in query:
            table = params[0]
            return [{"regclass": table if table in self._tables else None}]
        if "information_schema.columns" in query:
            table = params[0]
            cols = self._columns.get(table, set())
            return [{"column_name": c} for c in cols]
        if "sqlite_master" in query:
            table = params[0]
            return [{"name": table}] if table in self._tables else []
        if "PRAGMA table_info" in query:
            table = query.removeprefix("PRAGMA table_info(").removesuffix(")")
            cols = self._columns.get(table, set())
            return [{"name": c} for c in cols]
        raise AssertionError(f"unexpected query: {query}")


@pytest.mark.asyncio
async def test_assert_core_schema_ready_passes_when_tables_and_columns_present(mocker) -> None:
    columns = {table: set(cols) for table, cols in REQUIRED_CORE_COLUMNS.items()}
    conn = _FakeConn(
        dialect="postgres",
        existing_tables=set(REQUIRED_CORE_TABLES),
        columns=columns,
    )
    mocker.patch("common.db_startup.connections.get", return_value=conn)
    await assert_core_schema_ready()


@pytest.mark.asyncio
async def test_assert_core_schema_ready_passes_on_tortoise_sqlite_greenfield() -> None:
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["common.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        await assert_core_schema_ready()
    finally:
        await Tortoise.close_connections()


@pytest.mark.asyncio
async def test_assert_core_schema_ready_raises_when_table_missing(mocker) -> None:
    conn = _FakeConn(
        dialect="postgres",
        existing_tables={"saga_definitions"},
        columns={},
    )
    mocker.patch("common.db_startup.connections.get", return_value=conn)
    with pytest.raises(RuntimeError, match="missing tables"):
        await assert_core_schema_ready()


@pytest.mark.asyncio
async def test_assert_core_schema_ready_raises_when_column_missing(mocker) -> None:
    conn = _FakeConn(
        dialect="postgres",
        existing_tables=set(REQUIRED_CORE_TABLES),
        columns={"saga_step_instances": set()},
    )
    mocker.patch("common.db_startup.connections.get", return_value=conn)
    with pytest.raises(RuntimeError, match="missing columns"):
        await assert_core_schema_ready()
