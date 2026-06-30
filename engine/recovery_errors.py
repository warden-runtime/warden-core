"""Typed errors for operator saga recovery."""

from __future__ import annotations


class RecoveryError(Exception):
    """Base class for recovery failures."""


class RecoveryNotFoundError(RecoveryError):
    """Saga or step not found."""


class RecoveryConflictError(RecoveryError):
    """Preconditions not met for recovery."""


class RecoveryClaimActiveError(RecoveryError):
    """Worker claim is still active (non-stale) and force was not used."""

    def __init__(self, *, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"Active worker claim for idempotency_key={idempotency_key}")
