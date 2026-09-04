"""Read-side Tortoise queries for engine list APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.models import (
    SagaDefinition,
    SagaInstance,
    SagaStatus,
    SagaStepInstance,
    StepDefinition,
    StepStatus,
    WorkerDefinition,
)

if TYPE_CHECKING:
    import uuid

DEFAULT_LIMIT = 50
MAX_LIMIT = 100

_IN_FLIGHT_STATUSES: tuple[SagaStatus, ...] = (
    SagaStatus.PENDING,
    SagaStatus.RUNNING,
    SagaStatus.AWAITING_HUMAN,
    SagaStatus.COMPENSATING,
)


async def list_saga_definitions(
    *,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> list[SagaDefinition]:
    q: Any = SagaDefinition.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if name is not None:
        q = q.filter(name=name)
    if is_active is not None:
        q = q.filter(is_active=is_active)
    q = q.order_by("-updated_at", "-created_at", "id")
    return await q.offset(offset).limit(limit)


async def get_saga_definition_by_uuid(*, definition_id: uuid.UUID) -> SagaDefinition | None:
    return await SagaDefinition.filter(id=definition_id).first()


async def get_saga_definition_by_triple(
    *, namespace: str, name: str, version: str
) -> SagaDefinition | None:
    return await SagaDefinition.filter(namespace=namespace, name=name, version=version).first()


async def count_saga_definitions(
    *,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
) -> int:
    q: Any = SagaDefinition.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if name is not None:
        q = q.filter(name=name)
    if is_active is not None:
        q = q.filter(is_active=is_active)
    return await q.count()


async def list_worker_definitions(
    *,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> list[WorkerDefinition]:
    q: Any = WorkerDefinition.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if name is not None:
        q = q.filter(name=name)
    if is_active is not None:
        q = q.filter(is_active=is_active)
    q = q.order_by("-updated_at", "-created_at", "id")
    return await q.offset(offset).limit(limit)


async def get_worker_definition_by_uuid(*, definition_id: uuid.UUID) -> WorkerDefinition | None:
    return await WorkerDefinition.filter(id=definition_id).first()


async def get_worker_definition_by_triple(
    *, namespace: str, name: str, version: str
) -> WorkerDefinition | None:
    return await WorkerDefinition.filter(namespace=namespace, name=name, version=version).first()


async def count_worker_definitions(
    *,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
) -> int:
    q: Any = WorkerDefinition.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if name is not None:
        q = q.filter(name=name)
    if is_active is not None:
        q = q.filter(is_active=is_active)
    return await q.count()


async def list_step_definitions(
    *,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> list[StepDefinition]:
    q: Any = StepDefinition.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if name is not None:
        q = q.filter(name=name)
    if is_active is not None:
        q = q.filter(is_active=is_active)
    q = q.order_by("-updated_at", "-created_at", "id")
    return await q.offset(offset).limit(limit)


async def get_step_definition_by_uuid(*, definition_id: uuid.UUID) -> StepDefinition | None:
    return await StepDefinition.filter(id=definition_id).first()


async def get_step_definition_by_triple(
    *, namespace: str, name: str, version: str
) -> StepDefinition | None:
    return await StepDefinition.filter(namespace=namespace, name=name, version=version).first()


async def count_step_definitions(
    *,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
) -> int:
    q: Any = StepDefinition.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if name is not None:
        q = q.filter(name=name)
    if is_active is not None:
        q = q.filter(is_active=is_active)
    return await q.count()


async def count_saga_instances(
    *,
    namespace: str | None,
    trace_id: str | None,
    statuses: list[SagaStatus] | None,
    parent_trace_id: str | None = None,
) -> int:
    q: Any = SagaInstance.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if trace_id is not None:
        q = q.filter(trace_id=trace_id)
    if parent_trace_id is not None:
        q = q.filter(parent_trace_id=parent_trace_id)
    if statuses is not None:
        q = q.filter(status__in=statuses)
    return await q.count()


async def list_saga_instances(
    *,
    namespace: str | None,
    trace_id: str | None,
    statuses: list[SagaStatus] | None,
    limit: int,
    offset: int,
    parent_trace_id: str | None = None,
) -> list[SagaInstance]:
    q: Any = SagaInstance.all()
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if trace_id is not None:
        q = q.filter(trace_id=trace_id)
    if parent_trace_id is not None:
        q = q.filter(parent_trace_id=parent_trace_id)
    if statuses is not None:
        q = q.filter(status__in=statuses)
    q = q.order_by("-started_at", "trace_id")
    return await q.offset(offset).limit(limit)


async def list_saga_step_instances(
    *,
    saga_trace_id: str,
    namespace: str | None,
    statuses: list[StepStatus] | None,
    limit: int,
    offset: int,
) -> list[SagaStepInstance]:
    q: Any = SagaStepInstance.filter(saga_trace_id=saga_trace_id)
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if statuses is not None:
        q = q.filter(status__in=statuses)
    q = q.order_by("forward_seq", "span_id")
    return await q.offset(offset).limit(limit)


async def list_pending_review_steps(
    *,
    namespace: str | None,
    saga_trace_id: str | None,
    step_kind: str | None,
    limit: int,
    offset: int,
) -> list[SagaStepInstance]:
    q: Any = SagaStepInstance.filter(status=StepStatus.AWAITING_HUMAN)
    if namespace is not None:
        q = q.filter(namespace=namespace)
    if saga_trace_id is not None:
        q = q.filter(saga_trace_id=saga_trace_id)
    if step_kind is not None:
        q = q.filter(step_kind=step_kind)
    q = q.order_by("-started_at", "saga_trace_id", "order_index")
    return await q.offset(offset).limit(limit)


async def get_saga_instance(
    *,
    namespace: str | None,
    trace_id: str,
) -> SagaInstance | None:
    q: Any = SagaInstance.filter(trace_id=trace_id)
    if namespace is not None:
        q = q.filter(namespace=namespace)
    return await q.first()


async def get_saga_step_instance(
    *,
    saga_trace_id: str,
    step_span_id: str,
    namespace: str | None,
) -> SagaStepInstance | None:
    """Return one step row by composite key, or None if not found."""
    q: Any = SagaStepInstance.filter(
        saga_trace_id=saga_trace_id,
        span_id=step_span_id,
    )
    if namespace is not None:
        q = q.filter(namespace=namespace)
    return await q.first()


async def list_saga_step_instances_by_step_id(
    *,
    saga_trace_id: str,
    step_id: str,
    namespace: str | None,
) -> list[SagaStepInstance]:
    """Return all step rows for a manifest step_id (may be multiple on retry/compensation)."""
    q: Any = SagaStepInstance.filter(saga_trace_id=saga_trace_id, step_id=step_id)
    if namespace is not None:
        q = q.filter(namespace=namespace)
    return await q.order_by("-started_at", "-span_id")


def step_status_values() -> list[str]:
    return [s.value for s in StepStatus]


def parse_step_statuses(raw: list[str]) -> list[StepStatus]:
    out: list[StepStatus] = []
    for s in raw:
        try:
            out.append(StepStatus(s))
        except ValueError as e:
            raise ValueError(
                f"Invalid step status {s!r}. Valid values: {', '.join(step_status_values())}."
            ) from e
    return out


def saga_status_values() -> list[str]:
    return [s.value for s in SagaStatus]


def parse_saga_statuses(raw: list[str]) -> list[SagaStatus]:
    out: list[SagaStatus] = []
    for s in raw:
        try:
            out.append(SagaStatus(s))
        except ValueError as e:
            raise ValueError(
                f"Invalid saga status {s!r}. Valid values: {', '.join(saga_status_values())}."
            ) from e
    return out


def in_flight_statuses() -> list[SagaStatus]:
    return list(_IN_FLIGHT_STATUSES)
