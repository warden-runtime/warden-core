"""Step LLM token usage (provider-reported; never in user output).

Worker processes accumulate usage across ReAct / structured LLM calls and emit
``usage.worker`` on result events. The engine persists that envelope onto
``saga_step_instances.execution_usage``. Shape mirrors ``execution_timing``
(``{"worker": {...}}``) but is worker-only — the engine does not produce tokens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from common.agent_adapter import ExecutionStepError
from common.error_details import build_step_error_details
from common.execution_timing import clamp_nonneg

if TYPE_CHECKING:
    from common.llm import TokenUsage

WORKER_USAGE_COUNTERS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "llm_calls",
)

# Nested under execution_usage.worker.memory — compression layer counters (ints only).
WORKER_MEMORY_COUNTERS: tuple[str, ...] = (
    "compressions",
    "groups_evicted",
    "estimated_tokens_saved",
    "max_tier",
    "tier1_redactions",
)

# Stable aliases from LangChain / provider detail blobs → Warden keys.
_DETAIL_KEY_ALIASES: dict[str, str] = {
    "cache_read": "cache_read_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation": "cache_creation_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
    "cache_write": "cache_creation_tokens",
    "reasoning": "reasoning_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


def effective_max_step_tokens(step_value: int | None) -> int | None:
    """Resolve step YAML budget, falling back to WARDEN_MAX_STEP_TOKENS when unset.

    Returns None when unlimited (omit / null step field and env unset or 0).
    """
    if step_value is not None:
        return step_value if step_value > 0 else None
    raw = os.environ.get("WARDEN_MAX_STEP_TOKENS", "0") or "0"
    try:
        env = int(raw)
    except ValueError:
        return None
    return env if env > 0 else None


def enforce_step_token_budget(
    usage_acc: WorkerUsageAccumulator,
    max_step_tokens: int | None,
) -> None:
    """Abort the step when accumulated provider total_tokens exceed the budget."""
    if not max_step_tokens or usage_acc.total_tokens <= max_step_tokens:
        return
    message = (
        f"Step consumed {usage_acc.total_tokens} tokens, exceeding budget of {max_step_tokens}."
    )
    raise ExecutionStepError(
        f"Step token budget of {max_step_tokens} exceeded (consumed {usage_acc.total_tokens}).",
        error_details=build_step_error_details(
            code="STEP_TOKEN_LIMIT_EXCEEDED",
            message=message,
            tokens_used=usage_acc.total_tokens,
            max_step_tokens=max_step_tokens,
            prompt_tokens=usage_acc.prompt_tokens,
            completion_tokens=usage_acc.completion_tokens,
        ),
    )


def _normalize_detail_key(key: str) -> str | None:
    if not isinstance(key, str) or not key:
        return None
    if key in _DETAIL_KEY_ALIASES:
        return _DETAIL_KEY_ALIASES[key]
    # Pass through snake_case int-ish detail keys unchanged.
    if key.endswith("_tokens") or key in ("cache_read_tokens", "cache_creation_tokens"):
        return key
    return key if key.replace("_", "").isalnum() else None


def _coerce_nonneg_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return clamp_nonneg(int(value))
    except (TypeError, ValueError):
        return None


def _usage_is_reportable(usage: TokenUsage) -> bool:
    if usage.prompt_tokens > 0 or usage.completion_tokens > 0 or usage.total_tokens > 0:
        return True
    if usage.details:
        return True
    return bool(usage.model_id and usage.model_id.strip())


def _add_detail(out: dict[str, int], key: str | None, value: Any) -> None:
    if key is None:
        return
    if isinstance(value, dict):
        for nested_key, nested_val in normalize_usage_details(value).items():
            out[nested_key] = out.get(nested_key, 0) + nested_val
        return
    n = _coerce_nonneg_int(value)
    if n is not None and n > 0:
        out[key] = out.get(key, 0) + n


def normalize_usage_details(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize provider detail dicts to ``{key: nonneg int}`` (zeros omitted)."""
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, val in raw.items():
        _add_detail(out, _normalize_detail_key(str(key)), val)
    return {k: v for k, v in out.items() if v > 0}


def normalize_usage_memory(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize ``worker.memory`` compression counters (zeros omitted)."""
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in WORKER_MEMORY_COUNTERS:
        n = _coerce_nonneg_int(raw.get(key))
        if n is not None and n > 0:
            out[key] = n
    return out


def _normalize_worker_usage_dict(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in WORKER_USAGE_COUNTERS:
        n = _coerce_nonneg_int(raw.get(key))
        if n is not None and n > 0:
            out[key] = n
    model_id = raw.get("model_id")
    if isinstance(model_id, str) and model_id.strip():
        out["model_id"] = model_id.strip()
    details = normalize_usage_details(
        raw.get("details") if isinstance(raw.get("details"), dict) else None
    )
    if details:
        out["details"] = details
    memory = normalize_usage_memory(
        raw.get("memory") if isinstance(raw.get("memory"), dict) else None
    )
    if memory:
        out["memory"] = memory
    return out


def worker_usage_from_event(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Extract normalized worker usage from an event ``usage`` payload."""
    if not usage or not isinstance(usage, dict):
        return {}
    worker = usage.get("worker")
    if isinstance(worker, dict):
        return _normalize_worker_usage_dict(worker)
    return _normalize_worker_usage_dict(usage)


def merge_execution_usage(
    *,
    worker: dict[str, Any] | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge worker usage into persisted ``execution_usage`` shape."""
    merged: dict[str, Any] = {}
    if isinstance(existing, dict) and isinstance(existing.get("worker"), dict):
        merged["worker"] = _normalize_worker_usage_dict(existing["worker"])
    incoming = _normalize_worker_usage_dict(worker)
    if not incoming:
        return merged
    section = dict(merged.get("worker") or {})
    for key in WORKER_USAGE_COUNTERS:
        if key in incoming:
            section[key] = clamp_nonneg(int(incoming[key]))
    if isinstance(incoming.get("model_id"), str):
        section["model_id"] = incoming["model_id"]
    if isinstance(incoming.get("details"), dict):
        section["details"] = dict(incoming["details"])
    if isinstance(incoming.get("memory"), dict):
        # Replace with latest worker snapshot (accumulator already summed within the step).
        section["memory"] = dict(incoming["memory"])
    if section:
        merged["worker"] = section
    return merged


@dataclass
class WorkerUsageAccumulator:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    model_id: str | None = None
    details: dict[str, int] = field(default_factory=dict)
    memory: dict[str, int] = field(default_factory=dict)

    def add(self, usage: TokenUsage | None) -> None:
        """Accumulate one model invocation's provider-reported usage."""
        if usage is None or not _usage_is_reportable(usage):
            return
        self.prompt_tokens += clamp_nonneg(usage.prompt_tokens)
        self.completion_tokens += clamp_nonneg(usage.completion_tokens)
        self.total_tokens += clamp_nonneg(usage.total_tokens)
        self.llm_calls += 1
        model_id = (usage.model_id or "").strip()
        if model_id:
            self.model_id = model_id
        for key, val in normalize_usage_details(usage.details).items():
            self.details[key] = self.details.get(key, 0) + val
        self._mirror_counters()

    def add_memory_stats(self, stats: Any) -> None:
        """Accumulate compression-layer counters from a CompressionStats-like object."""
        to_mem = getattr(stats, "to_usage_memory", None)
        incoming = to_mem() if callable(to_mem) else None
        if not isinstance(incoming, dict) or not incoming:
            return
        for key, val in normalize_usage_memory(incoming).items():
            if key == "max_tier":
                self.memory[key] = max(self.memory.get(key, 0), val)
            else:
                self.memory[key] = self.memory.get(key, 0) + val

    def _mirror_counters(self) -> None:
        from common.telemetry import record_usage_counter_on_current_span

        for key in WORKER_USAGE_COUNTERS:
            record_usage_counter_on_current_span(
                section="worker",
                key=key,
                value=getattr(self, key),
            )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            key: clamp_nonneg(getattr(self, key))
            for key in WORKER_USAGE_COUNTERS
            if getattr(self, key) > 0
        }
        if self.model_id:
            out["model_id"] = self.model_id
        details = {k: v for k, v in self.details.items() if v > 0}
        if details:
            out["details"] = details
        memory = normalize_usage_memory(self.memory)
        if memory:
            out["memory"] = memory
        return out

    def to_wire(self) -> dict[str, Any]:
        buckets = self.to_dict()
        return {"worker": buckets} if buckets else {}
