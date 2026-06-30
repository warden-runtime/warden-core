"""PostgreSQL-backed tests (paired reap SKIP LOCKED, migration SQL).

Requires Docker for ephemeral Postgres via testcontainers, or set
``WARDEN_TEST_POSTGRES_URL`` to an existing database (schema reset per test).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tortoise import Tortoise
from tortoise.backends.base.executor import EXECUTOR_CACHE

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _REPO_ROOT / "db" / "migrations"

_TRUNCATE_SQL = """
TRUNCATE TABLE
  processed_ingest_events,
  processed_commands,
  outbox_events,
  saga_step_instances,
  saga_instances,
  provider_secrets,
  worker_definitions,
  saga_definitions
RESTART IDENTITY CASCADE;
"""


def _postgres_url_from_env() -> str | None:
    url = os.environ.get("WARDEN_TEST_POSTGRES_URL", "").strip()
    return url or None


def _normalize_postgres_url_for_tortoise(url: str) -> str:
    """Tortoise asyncpg accepts ``postgres://`` / ``postgresql://``, not ``postgresql+psycopg2://``."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    base_scheme = scheme.split("+", 1)[0].lower()
    if base_scheme in ("postgresql", "postgres"):
        return f"postgres://{rest}"
    return url


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """Ephemeral Postgres (testcontainers) or WARDEN_TEST_POSTGRES_URL."""
    env_url = _postgres_url_from_env()
    if env_url:
        yield _normalize_postgres_url_for_tortoise(env_url)
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        pytest.fail(
            "Postgres tests require testcontainers (uv sync --extra dev) "
            f"or WARDEN_TEST_POSTGRES_URL: {exc}"
        )

    try:
        with PostgresContainer("postgres:16") as postgres:
            yield _normalize_postgres_url_for_tortoise(postgres.get_connection_url())
    except Exception as exc:
        pytest.fail(
            "Postgres tests require a running Docker daemon or WARDEN_TEST_POSTGRES_URL. "
            f"Could not start Postgres container: {exc}"
        )


@pytest.fixture(autouse=True)
async def initialize_tests(postgres_url: str):
    """Override SQLite autouse fixture from tests/conftest.py for this package."""
    await Tortoise.close_connections()
    # Tortoise caches INSERT SQL by (connection_name, schema, table) only — not backend.
    # After hundreds of SQLite tests, stale ``?`` placeholders break asyncpg inserts.
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=postgres_url,
        modules={"models": ["common.models"]},
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


@pytest.fixture(autouse=True)
async def _truncate_tables(request: pytest.FixtureRequest):
    """Clean slate per test on shared Postgres."""
    yield
    if request.node.get_closest_marker("schema_migration"):
        return
    conn = Tortoise.get_connection("default")
    await conn.execute_script(_TRUNCATE_SQL)


@pytest.fixture
def migrations_dir() -> Path:
    return _MIGRATIONS_DIR
