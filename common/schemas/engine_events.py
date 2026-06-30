"""Engine lifecycle event_type wire strings for hook kwargs (OSS kernel)."""

from __future__ import annotations

from enum import StrEnum


class AuditEngineEventType(StrEnum):
    """Engine lifecycle audit event_type wire strings (v1)."""

    SAGA_CREATED = "saga.created"
    STEP_CREATED = "step.created"
    SAGA_STARTED = "saga.started"
    STEP_SCHEDULED = "step.scheduled"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    STEP_TIMED_OUT = "step.timed_out"
    SAGA_COMPENSATING = "saga.compensating"
    COMPENSATION_SCHEDULED = "compensation.scheduled"
    STEP_COMPENSATED = "step.compensated"
    SAGA_COMPENSATED = "saga.compensated"
    SAGA_COMPLETED = "saga.completed"
    SAGA_FAILED = "saga.failed"
    SAGA_STEPS_SKIPPED = "saga.steps_skipped"
    STEP_AWAITING_HUMAN = "step.awaiting_human"
    SAGA_AWAITING_HUMAN = "saga.awaiting_human"
    STEP_RESUMED_FROM_HITL = "step.resumed_from_hitl"
    SAGA_RESUMED_FROM_HITL = "saga.resumed_from_hitl"
    INGEST_DEDUPLICATED = "engine.ingest_deduplicated"
