from unittest.mock import MagicMock

from common.compensation import forward_eligible_for_compensation, forward_step_has_compensation
from common.models import StepStatus


def _forward_stub(**kwargs):
    stub = MagicMock()
    stub.compensation_definition = kwargs.get("compensation_definition")
    stub.status = kwargs.get("status", StepStatus.COMPLETED)
    stub.step_kind = kwargs.get("step_kind", "reason")
    return stub


def test_forward_step_has_compensation_false_when_none() -> None:
    assert forward_step_has_compensation(_forward_stub(compensation_definition=None)) is False


def test_forward_step_has_compensation_true_when_declared() -> None:
    assert (
        forward_step_has_compensation(
            _forward_stub(compensation_definition={"worker": "w", "worker_version": "1"})
        )
        is True
    )


def test_spawn_always_has_compensation() -> None:
    assert (
        forward_step_has_compensation(
            _forward_stub(step_kind="spawn_sagas", compensation_definition=None)
        )
        is True
    )


def test_join_skips_compensation() -> None:
    assert (
        forward_step_has_compensation(
            _forward_stub(step_kind="join_sagas", compensation_definition={"worker": "w"})
        )
        is False
    )


def test_forward_eligible_for_compensation_includes_in_progress() -> None:
    assert forward_eligible_for_compensation(_forward_stub(status=StepStatus.IN_PROGRESS)) is True


def test_forward_eligible_for_compensation_excludes_pending() -> None:
    assert forward_eligible_for_compensation(_forward_stub(status=StepStatus.PENDING)) is False
