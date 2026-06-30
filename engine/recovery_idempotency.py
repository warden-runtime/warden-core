"""Operator recovery HTTP idempotency (client-supplied recovery_token)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from common.models import ProcessedOperatorRecovery
from common.utils import hash_canonical_dict
from tortoise.transactions import in_transaction

from engine.recovery_errors import RecoveryConflictError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tortoise.backends.base.client import BaseDBAsyncClient


def operator_recovery_dedup_key(
    *,
    namespace: str,
    recovery_kind: str,
    trace_id: str,
    step_span_id: str,
    recovery_token: str,
) -> str:
    return (
        f"operator-recovery:{recovery_kind}:{namespace}:{trace_id}:{step_span_id}:{recovery_token}"
    )


def operator_recovery_request_fingerprint(
    *,
    recovery_kind: str,
    force: bool,
    allow_destructive: bool | None = None,
) -> str:
    payload: dict[str, object] = {
        "recovery_kind": recovery_kind,
        "force": force,
    }
    if allow_destructive is not None:
        payload["allow_destructive"] = allow_destructive
    return hash_canonical_dict(payload)


async def load_processed_operator_recovery(
    dedup_key: str,
    *,
    conn: BaseDBAsyncClient,
) -> ProcessedOperatorRecovery | None:
    return (
        await ProcessedOperatorRecovery.filter(dedup_key=dedup_key)
        .using_db(conn)
        .select_for_update()
        .first()
    )


async def save_processed_operator_recovery(
    *,
    dedup_key: str,
    request_fingerprint: str,
    response_json: dict[str, str],
    conn: BaseDBAsyncClient,
) -> None:
    await ProcessedOperatorRecovery.create(
        dedup_key=dedup_key,
        request_fingerprint=request_fingerprint,
        response_json=response_json,
        using_db=conn,
    )


def replay_or_conflict(
    existing: ProcessedOperatorRecovery,
    *,
    request_fingerprint: str,
) -> dict[str, str]:
    if existing.request_fingerprint != request_fingerprint:
        raise RecoveryConflictError("recovery_token reused with different request parameters.")
    return cast("dict[str, str]", existing.response_json)


async def with_operator_recovery_idempotency(
    *,
    recovery_token: str | None,
    namespace: str,
    recovery_kind: str,
    trace_id: str,
    step_span_id: str,
    force: bool,
    allow_destructive: bool | None,
    apply: Callable[[BaseDBAsyncClient], Awaitable[dict[str, str]]],
) -> dict[str, str]:
    """Run recovery once per client token; replay stored response on duplicate."""
    if recovery_token is None:
        async with in_transaction() as conn:
            return await apply(conn)

    dedup_key = operator_recovery_dedup_key(
        namespace=namespace,
        recovery_kind=recovery_kind,
        trace_id=trace_id,
        step_span_id=step_span_id,
        recovery_token=recovery_token,
    )
    fingerprint = operator_recovery_request_fingerprint(
        recovery_kind=recovery_kind,
        force=force,
        allow_destructive=allow_destructive,
    )
    async with in_transaction() as conn:
        existing = await load_processed_operator_recovery(dedup_key, conn=conn)
        if existing is not None:
            return replay_or_conflict(existing, request_fingerprint=fingerprint)
        result = await apply(conn)
        await save_processed_operator_recovery(
            dedup_key=dedup_key,
            request_fingerprint=fingerprint,
            response_json=result,
            conn=conn,
        )
        return result
