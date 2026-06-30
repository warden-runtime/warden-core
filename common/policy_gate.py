"""CEL-only policy gate evaluation (no ledger writes)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from common.config import get_settings
from common.policy.cel_eval import PolicyEvaluationError, compile_cel_program, evaluate_cel_bool
from common.policy.loader import PolicyArtifact, load_policy_artifact
from common.utils import hash_canonical_dict

if TYPE_CHECKING:
    from tortoise.backends.base.client import BaseDBAsyncClient

    from common.schemas.policy import PolicyDenialCode, PolicyPhase

__all__ = [
    "PolicyGateOutcome",
    "PolicyGateResult",
    "artifact_source_hash",
    "binding_hash",
    "dispatch_policy_gate_hooks",
    "evaluate_policy_gate",
    "run_policy_gate",
]

logger = logging.getLogger(__name__)

_compiled_cel_cache: dict[str, object] = {}


class PolicyGateOutcome(StrEnum):
    PASSED = "passed"
    DENIED = "denied"
    ERRORED = "errored"


@dataclass(frozen=True)
class PolicyGateResult:
    outcome: PolicyGateOutcome
    denial_code: PolicyDenialCode | None = None
    error_code: str | None = None
    error_message: str | None = None
    policy_name: str | None = None
    policy_version: str | None = None
    artifact_source_hash: str | None = None
    binding_hash: str | None = None


def artifact_source_hash(artifact: PolicyArtifact) -> str:
    return hash_canonical_dict(
        {
            "name": artifact.name,
            "version": artifact.version,
            "cel": artifact.cel_source,
        }
    )


def binding_hash(binding: dict[str, Any]) -> str:
    return hash_canonical_dict(binding)


def _compiled_cel(source: str) -> object:
    runner = _compiled_cel_cache.get(source)
    if runner is None:
        runner = compile_cel_program(source)
        _compiled_cel_cache[source] = runner
    return runner


async def evaluate_policy_gate(
    *,
    policy_name: str,
    phase: PolicyPhase,
    binding: dict[str, Any],
    denial_code: PolicyDenialCode,
    policies_root: str | None = None,
) -> PolicyGateResult:
    """Load policy artifact and evaluate CEL; no audit rows."""
    root = policies_root if policies_root is not None else get_settings().policies_root
    name = policy_name.strip()
    if not name:
        return PolicyGateResult(
            outcome=PolicyGateOutcome.ERRORED,
            error_code="POLICY_EVALUATION_FAILED",
            error_message="policy_name is empty",
        )

    b_hash = binding_hash(binding)
    try:
        artifact = await load_policy_artifact(policies_root=root, policy_name=name)
    except FileNotFoundError as e:
        return PolicyGateResult(
            outcome=PolicyGateOutcome.ERRORED,
            policy_name=name,
            error_code="POLICY_EVALUATION_FAILED",
            error_message=str(e),
        )
    except ValueError as e:
        return PolicyGateResult(
            outcome=PolicyGateOutcome.ERRORED,
            policy_name=name,
            error_code="POLICY_EVALUATION_FAILED",
            error_message=str(e),
        )

    src_hash = artifact_source_hash(artifact)
    try:
        ok = evaluate_cel_bool(
            artifact=artifact,
            cel_program=_compiled_cel(artifact.cel_source),
            binding=binding,
        )
    except PolicyEvaluationError as e:
        logger.exception(
            "Policy gate phase=%s name=%s evaluation failed: %s",
            phase,
            artifact.name,
            e,
        )
        return PolicyGateResult(
            outcome=PolicyGateOutcome.ERRORED,
            policy_name=artifact.name,
            error_code="POLICY_EVALUATION_FAILED",
            error_message=str(e),
        )

    logger.info(
        "policy gate phase=%s name=%s version=%s result=%s",
        phase,
        artifact.name,
        artifact.version,
        ok,
    )

    if ok:
        return PolicyGateResult(
            outcome=PolicyGateOutcome.PASSED,
            policy_name=artifact.name,
            policy_version=artifact.version,
            artifact_source_hash=src_hash,
            binding_hash=b_hash,
        )

    return PolicyGateResult(
        outcome=PolicyGateOutcome.DENIED,
        denial_code=denial_code,
        policy_name=artifact.name,
        policy_version=artifact.version,
        artifact_source_hash=src_hash,
        binding_hash=b_hash,
    )


async def dispatch_policy_gate_hooks(
    *,
    result: PolicyGateResult,
    phase: PolicyPhase,
    binding: dict[str, Any],
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    conn: BaseDBAsyncClient | None = None,
    trace_context: dict[str, Any] | None = None,
) -> None:
    """Invoke registry policy hooks for the evaluation outcome (fail-open NoOp by default)."""
    from common.plugins.registry import get_registry

    hooks = get_registry().policy
    hook_kwargs = {
        "phase": phase,
        "binding": binding,
        "result": result,
        "namespace": namespace,
        "saga_trace_id": saga_trace_id,
        "step_span_id": step_span_id,
        "conn": conn,
        "trace_context": trace_context,
    }
    if result.outcome == PolicyGateOutcome.PASSED:
        await hooks.on_evaluated(**hook_kwargs)
        return
    if result.outcome == PolicyGateOutcome.DENIED:
        await hooks.on_denied(**hook_kwargs)
        return
    if result.policy_name is not None:
        await hooks.on_errored(**hook_kwargs)


async def run_policy_gate(
    *,
    policy_name: str,
    phase: PolicyPhase,
    binding: dict[str, Any],
    denial_code: PolicyDenialCode,
    namespace: str,
    saga_trace_id: str,
    step_span_id: str,
    conn: BaseDBAsyncClient | None = None,
    trace_context: dict[str, Any] | None = None,
    policies_root: str | None = None,
) -> PolicyGateResult:
    """Evaluate CEL and dispatch policy gate hooks; returns evaluation result."""
    result = await evaluate_policy_gate(
        policy_name=policy_name,
        phase=phase,
        binding=binding,
        denial_code=denial_code,
        policies_root=policies_root,
    )
    await dispatch_policy_gate_hooks(
        result=result,
        phase=phase,
        binding=binding,
        namespace=namespace,
        saga_trace_id=saga_trace_id,
        step_span_id=step_span_id,
        conn=conn,
        trace_context=trace_context,
    )
    return result
