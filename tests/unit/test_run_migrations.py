"""Unit tests for scripts/run_migrations.py (no database)."""

import re

from scripts.run_migrations import list_migration_files

_MIGRATION_NAME = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")


def test_list_migration_files_sorted_by_name() -> None:
    files = list_migration_files()
    names = [p.name for p in files]
    assert names, "expected at least one migration under db/migrations/"
    assert names == sorted(names)
    assert names[0] == "000_initial_schema.sql"
    for name in names:
        assert _MIGRATION_NAME.match(name), f"unexpected migration filename: {name}"


def test_migration_filenames_are_unique() -> None:
    files = list_migration_files()
    stems = [p.stem for p in files]
    assert stems
    assert len(stems) == len(set(stems))
