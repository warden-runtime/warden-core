"""Unit tests for common.worker_ref helpers."""

import pytest
from common.worker_ref import assert_worker_snapshot_version


def test_assert_worker_snapshot_version_accepts_matching_snapshot() -> None:
    assert_worker_snapshot_version({"version": "1.0.0"}, expected_version="1.0.0")


def test_assert_worker_snapshot_version_ignores_missing_snapshot() -> None:
    assert_worker_snapshot_version(None, expected_version="1.0.0")


def test_assert_worker_snapshot_version_rejects_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        assert_worker_snapshot_version({"version": "1.0.0"}, expected_version="2.0.0")
