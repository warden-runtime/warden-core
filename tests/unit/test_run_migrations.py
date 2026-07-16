"""Unit tests for scripts/run_migrations.py (no database)."""

from scripts.run_migrations import list_migration_files


def test_list_migration_files_sorted_by_name() -> None:
    files = list_migration_files()
    names = [p.name for p in files]
    assert names == ["000_initial_schema.sql", "001_execution_usage.sql"]
    assert names == sorted(names)


def test_migration_filenames_are_unique() -> None:
    files = list_migration_files()
    stems = [p.stem for p in files]
    assert len(stems) == len(set(stems))
    assert stems == ["000_initial_schema", "001_execution_usage"]
