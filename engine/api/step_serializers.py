"""Map SagaStepInstance ORM rows to engine read API schemas."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.api.schemas import SagaStepInstanceDetail, SagaStepInstanceItem

if TYPE_CHECKING:
    from common.models import SagaStepInstance


def saga_step_instance_item_from_row(row: SagaStepInstance) -> SagaStepInstanceItem:
    """Build list-item schema from a saga step instance row."""
    return SagaStepInstanceItem(
        step_span_id=row.span_id,
        saga_trace_id=row.saga_trace_id,
        namespace=row.namespace,
        step_id=row.step_id,
        step_name=row.step_name,
        step_kind=row.step_kind,
        order_index=row.order_index,
        status=row.status.value,
        worker=row.worker,
        worker_version=row.worker_version,
        started_at=row.started_at,
        end_time=row.end_time,
        compensates_span_id=row.compensates_span_id,
        error_details=row.error_details,
        timing=row.execution_timing,
        usage=row.execution_usage,
    )


def saga_step_instance_detail_from_row(row: SagaStepInstance) -> SagaStepInstanceDetail:
    """Build detail schema from a saga step instance row."""
    base = saga_step_instance_item_from_row(row)
    return SagaStepInstanceDetail(
        **base.model_dump(),
        resolved_arguments=row.resolved_arguments,
        output_payload=row.output_payload,
        prompt_ref=row.prompt_ref,
    )
