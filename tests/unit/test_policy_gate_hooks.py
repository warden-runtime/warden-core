"""Policy gate hook dispatch (no full saga DB)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from common.plugins import register_policy_hooks, reset_registry
from common.policy_gate import PolicyGateOutcome, run_policy_gate


@dataclass
class _CountingPolicyHooks:
    evaluated: int = 0
    denied: int = 0
    errored: int = 0
    calls: list[str] = field(default_factory=list)

    def get_required_modules(self) -> list[str]:
        return []

    async def on_evaluated(self, **kwargs: object) -> None:
        self.evaluated += 1
        self.calls.append("evaluated")

    async def on_denied(self, **kwargs: object) -> None:
        self.denied += 1
        self.calls.append("denied")

    async def on_errored(self, **kwargs: object) -> None:
        self.errored += 1
        self.calls.append("errored")


@pytest.fixture(autouse=True)
def _isolated_policy_hooks():
    reset_registry()
    yield
    reset_registry()


@pytest.mark.asyncio
async def test_run_policy_gate_dispatches_pass_to_on_evaluated(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICIES_ROOT", str(tmp_path))
    (tmp_path / "ok.yaml").write_text(
        'name: ok\nversion: "1"\ncel: "true"\n',
        encoding="utf-8",
    )
    hooks = _CountingPolicyHooks()
    register_policy_hooks(hooks)

    result = await run_policy_gate(
        policy_name="ok",
        phase="after_reason",
        binding={"output": {}},
        denial_code="POLICY_REASON_DENIED",
        namespace="default",
        saga_trace_id="a" * 32,
        step_span_id="b" * 16,
        policies_root=str(tmp_path),
    )

    assert result.outcome == PolicyGateOutcome.PASSED
    assert hooks.evaluated == 1
    assert hooks.denied == 0
    assert hooks.errored == 0


@pytest.mark.asyncio
async def test_run_policy_gate_empty_name_skips_on_errored():
    hooks = _CountingPolicyHooks()
    register_policy_hooks(hooks)

    result = await run_policy_gate(
        policy_name="  ",
        phase="before_commit",
        binding={},
        denial_code="POLICY_COMMIT_DENIED",
        namespace="default",
        saga_trace_id="a" * 32,
        step_span_id="b" * 16,
    )

    assert result.outcome == PolicyGateOutcome.ERRORED
    assert hooks.errored == 0
