"""Evaluate CEL programs to a boolean against a variable binding (e.g. step output envelope)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import celpy
from celpy.adapter import json_to_cel
from celpy.celparser import CELParseError
from celpy.celtypes import BoolType
from celpy.evaluation import CELEvalError

if TYPE_CHECKING:
    from common.policy.loader import PolicyArtifact

logger = logging.getLogger(__name__)


class PolicyEvaluationError(Exception):
    """CEL parse, type, or evaluation failed for a policy gate."""


class PolicyCommitDenied(Exception):
    """Policy ``cel`` evaluated to false for a commit step; ``DO_COMMIT`` must not be queued."""


def _cel_bool_to_python(value: Any) -> bool:
    if value is True or value is False:
        return bool(value)
    if isinstance(value, BoolType):
        return bool(value)
    value_type = type(value)
    if hasattr(value, "__bool__") and getattr(value_type, "__module__", "").startswith("celpy"):
        return bool(value)
    raise PolicyEvaluationError(f"CEL policy must evaluate to bool, got {value_type.__name__}")


def evaluate_cel_bool(
    *,
    artifact: PolicyArtifact,
    cel_program: Any,
    binding: dict[str, Any],
) -> bool:
    """Run compiled CEL program with ``binding`` as top-level variable names (CEL context).

    Args:
        artifact: For logging only.
        cel_program: Compiled runner from :meth:`compile_cel_program`.
        binding: Map of root identifiers to JSON-like dicts (e.g. ``{"output": {...}}``).

    Returns:
        True if the expression evaluates to true; False if not.

    Raises:
        PolicyEvaluationError: On evaluation errors.
    """
    try:
        ctx = {key: json_to_cel(val) for key, val in binding.items()}
    except (TypeError, ValueError) as e:
        logger.exception("Policy %s: failed to adapt binding: %s", artifact.name, e)
        raise PolicyEvaluationError(str(e)) from e

    try:
        result = cel_program.evaluate(ctx)
    except CELEvalError as e:
        logger.exception("Policy %s: CEL evaluation error: %s", artifact.name, e)
        raise PolicyEvaluationError(str(e)) from e
    except Exception as e:
        logger.exception("Policy %s: unexpected error: %s", artifact.name, e)
        raise PolicyEvaluationError(str(e)) from e

    return _cel_bool_to_python(result)


def compile_cel_program(source: str) -> Any:
    """Compile CEL source to a reusable runner.

    Args:
        source: CEL expression text.

    Raises:
        PolicyEvaluationError: On parse errors.
    """
    env = celpy.Environment()
    try:
        ast = env.compile(source)
        return env.program(ast)
    except CELParseError as e:
        raise PolicyEvaluationError(f"CEL parse error: {e}") from e
