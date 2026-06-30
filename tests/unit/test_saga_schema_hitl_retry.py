"""Saga manifest schema: HITL retry fields."""

from common.schemas.saga import ReasonSagaStep


def test_reason_step_accepts_hitl_retry_fields():
    step = ReasonSagaStep.model_validate(
        {
            "id": "review",
            "name": "review",
            "kind": "reason",
            "worker": "w",
            "worker_version": "1.0.0",
            "prompt": "p.j2",
            "hitl": True,
            "hitl_max_retries": 3,
            "hitl_retry_guidance": "Re-check fraud signals before approving.",
        }
    )
    assert step.hitl is True
    assert step.hitl_max_retries == 3
    assert "fraud" in (step.hitl_retry_guidance or "")
