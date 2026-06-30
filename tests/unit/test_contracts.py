"""Contract model validation boundaries."""

import pytest
from common.contracts import (
    DoStepCommand,
    HumanApprovedIngestEvent,
    HumanRejectedIngestEvent,
    HumanRetryIngestEvent,
    SagaStartedEvent,
    StepCompletedIngestEvent,
    coerce_saga_ingest_dict,
    coerce_worker_command_dict,
)
from pydantic import ValidationError


def test_worker_command_rejects_envelope_extras() -> None:
    with pytest.raises(ValidationError):
        DoStepCommand(
            type="DO_STEP",
            namespace="default",
            saga_trace_id="a" * 32,
            step_span_id="b" * 16,
            worker_name="w",
            worker_version="1.0.0",
            idempotency_key="idem-key-01",
            prompt_ref="p.j2",
            event_type="DO_STEP",
        )


def test_coerce_worker_command_dict_strips_envelope_keys() -> None:
    wire = coerce_worker_command_dict(
        {
            "type": "DO_STEP",
            "event_type": "DO_STEP",
            "trace_context": {},
            "namespace": "default",
            "saga_trace_id": "a" * 32,
            "step_span_id": "b" * 16,
            "worker_name": "w",
            "worker_version": "1.0.0",
            "idempotency_key": "idem-key-01",
            "prompt_ref": "p.j2",
        }
    )
    cmd = DoStepCommand(**wire)
    assert cmd.worker_name == "w"
    assert cmd.prompt_ref == "p.j2"


def test_coerce_saga_ingest_dict_strips_worker_type_key() -> None:
    data = coerce_saga_ingest_dict(
        {
            "type": "STEP_COMPLETED",
            "event_type": "STEP_COMPLETED",
            "namespace": "default",
            "saga_trace_id": "a" * 32,
            "step_span_id": "b" * 16,
            "output": {},
        }
    )
    event = StepCompletedIngestEvent.model_validate(data)
    assert event.event_type == "STEP_COMPLETED"


def test_coerce_saga_ingest_dict_strips_saga_event_payload_notification_fields() -> None:
    """SagaEventPayload rows include status/output; SagaStartedEvent ingest does not."""
    data = coerce_saga_ingest_dict(
        {
            "event_type": "SAGA_STARTED",
            "namespace": "default",
            "saga_trace_id": "a" * 32,
            "step_span_id": None,
            "status": "PENDING",
            "output": {},
        }
    )
    event = SagaStartedEvent.model_validate(data)
    assert event.event_type == "SAGA_STARTED"
    assert "status" not in data
    assert "output" not in data


def test_coerce_saga_ingest_dict_strips_human_approved_outbox_fields() -> None:
    """HumanApprovedOutboxPayload includes idempotency_key; ingest model does not."""
    data = coerce_saga_ingest_dict(
        {
            "event_type": "HUMAN_APPROVED",
            "namespace": "default",
            "saga_trace_id": "a" * 32,
            "step_span_id": "b" * 16,
            "idempotency_key": "human-decision-" + "a" * 32 + "-b" * 8,
            "output": None,
        }
    )
    event = HumanApprovedIngestEvent.model_validate(data)
    assert event.event_type == "HUMAN_APPROVED"
    assert "idempotency_key" not in data


def test_coerce_saga_ingest_dict_strips_human_rejected_outbox_fields() -> None:
    data = coerce_saga_ingest_dict(
        {
            "event_type": "HUMAN_REJECTED",
            "namespace": "default",
            "saga_trace_id": "a" * 32,
            "step_span_id": "b" * 16,
            "idempotency_key": "human-decision-" + "a" * 32 + "-b" * 8,
            "error_details": {"reason": "operator rejected"},
        }
    )
    event = HumanRejectedIngestEvent.model_validate(data)
    assert event.event_type == "HUMAN_REJECTED"
    assert event.error_details == {"reason": "operator rejected"}
    assert "idempotency_key" not in data


def test_coerce_saga_ingest_dict_strips_human_retry_outbox_fields() -> None:
    data = coerce_saga_ingest_dict(
        {
            "event_type": "HUMAN_RETRY",
            "namespace": "default",
            "saga_trace_id": "a" * 32,
            "step_span_id": "b" * 16,
            "idempotency_key": "human-retry-" + "a" * 32 + "-b" * 8 + "-tok",
            "retry_guidance": "Re-check the issue list.",
        }
    )
    event = HumanRetryIngestEvent.model_validate(data)
    assert event.event_type == "HUMAN_RETRY"
    assert event.retry_guidance == "Re-check the issue list."
    assert "idempotency_key" not in data
