"""Step execution timing buckets (monotonic per-process; never in user output).

Worker processes record local ``perf_counter`` segments and emit ``timing.worker`` on
result events. The engine merges engine-side buckets at ingest into
``saga_step_instances.execution_timing``. Staging uses ``pending_engine_timing``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

DISPATCH_PERF_ANCHOR_KEY = "_dispatch_perf_anchor"

WORKER_BUCKETS: tuple[str, ...] = (
    "hydration_ms",
    "setup_ms",
    "llm_ms",
    "tool_ms",
)

ENGINE_BUCKETS: tuple[str, ...] = (
    "when_cel_ms",
    "schedule_ms",
    "policy_ms",
    "dispatch_to_ingest_ms",
)

# Reject perf_counter anchors that imply > 24h (stale after restart).
_MAX_DISPATCH_ANCHOR_MS = 86_400_000


def clamp_nonneg(ms: int) -> int:
    return max(0, int(ms))


def elapsed_ms(since: float) -> int:
    return clamp_nonneg(int((time.perf_counter() - since) * 1000))


def _normalize_bucket_dict(
    raw: dict[str, Any] | None, *, allowed: tuple[str, ...]
) -> dict[str, int]:
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in allowed:
        val = raw.get(key)
        if val is None:
            continue
        try:
            normalized = clamp_nonneg(int(val))
        except (TypeError, ValueError):
            continue
        if normalized > 0:
            out[key] = normalized
    return out


def worker_timing_from_event(timing: dict[str, Any] | None) -> dict[str, int]:
    if not timing or not isinstance(timing, dict):
        return {}
    worker = timing.get("worker")
    if isinstance(worker, dict):
        return _normalize_bucket_dict(worker, allowed=WORKER_BUCKETS)
    return _normalize_bucket_dict(timing, allowed=WORKER_BUCKETS)


def engine_timing_from_pending(pending: dict[str, Any] | None) -> dict[str, int]:
    if not pending or not isinstance(pending, dict):
        return {}
    engine = pending.get("engine")
    if isinstance(engine, dict):
        return _normalize_bucket_dict(engine, allowed=ENGINE_BUCKETS)
    return _normalize_bucket_dict(pending, allowed=ENGINE_BUCKETS)


def _copy_existing_timing(existing: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if not existing or not isinstance(existing, dict):
        return merged
    if isinstance(existing.get("worker"), dict):
        merged["worker"] = dict(existing["worker"])
    if isinstance(existing.get("engine"), dict):
        merged["engine"] = dict(existing["engine"])
    return merged


def _apply_bucket_group(
    merged: dict[str, Any],
    *,
    section: str,
    buckets: dict[str, int] | None,
    allowed: tuple[str, ...],
) -> None:
    if not buckets:
        return
    section_dict = dict(merged.get(section) or {})
    for key, val in buckets.items():
        if key in allowed:
            section_dict[key] = clamp_nonneg(int(val))
    if section_dict:
        merged[section] = section_dict


def merge_execution_timing(
    *,
    worker: dict[str, int] | None = None,
    engine: dict[str, int] | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge worker/engine bucket dicts into persisted ``execution_timing`` shape."""
    merged = _copy_existing_timing(existing)
    _apply_bucket_group(merged, section="worker", buckets=worker, allowed=WORKER_BUCKETS)
    _apply_bucket_group(merged, section="engine", buckets=engine, allowed=ENGINE_BUCKETS)
    return merged


def compute_dispatch_to_ingest_ms(
    *,
    anchor: float | None,
    outbox_created_at: datetime | None,
    now: datetime | None = None,
) -> int | None:
    """Engine-observed async latency from command outbox commit to ingest handler entry."""
    if anchor is not None:
        ms = elapsed_ms(anchor)
        if 0 <= ms <= _MAX_DISPATCH_ANCHOR_MS:
            return ms
    if outbox_created_at is not None:
        ref = now if now is not None else datetime.now(UTC)
        created = outbox_created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        delta_ms = int((ref - created).total_seconds() * 1000)
        if delta_ms >= 0:
            return clamp_nonneg(delta_ms)
    return None


def dispatch_anchor_from_pending(pending: dict[str, Any] | None) -> float | None:
    if not pending or not isinstance(pending, dict):
        return None
    raw = pending.get(DISPATCH_PERF_ANCHOR_KEY)
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def pending_engine_payload(
    engine_buckets: dict[str, int], *, dispatch_anchor: float | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"engine": dict(engine_buckets)}
    if dispatch_anchor is not None:
        payload[DISPATCH_PERF_ANCHOR_KEY] = dispatch_anchor
    return payload


def merge_pending_engine(
    existing: dict[str, Any] | None,
    *,
    engine_add: dict[str, int] | None = None,
    dispatch_anchor: float | None = None,
) -> dict[str, Any]:
    base = dict(existing) if isinstance(existing, dict) else {}
    engine = engine_timing_from_pending(base)
    if engine_add:
        for key, val in engine_add.items():
            if key in ENGINE_BUCKETS:
                engine[key] = engine.get(key, 0) + clamp_nonneg(int(val))
    out: dict[str, Any] = {"engine": engine}
    anchor = dispatch_anchor if dispatch_anchor is not None else dispatch_anchor_from_pending(base)
    if anchor is not None:
        out[DISPATCH_PERF_ANCHOR_KEY] = anchor
    return out


@dataclass
class WorkerTimingAccumulator:
    hydration_ms: int = 0
    setup_ms: int = 0
    llm_ms: int = 0
    tool_ms: int = 0
    _marks: dict[str, float] = field(default_factory=dict)

    def start(self, label: str) -> None:
        self._marks[label] = time.perf_counter()

    def stop(self, label: str, *, bucket: str) -> None:
        since = self._marks.pop(label, None)
        if since is None or bucket not in WORKER_BUCKETS:
            return
        current = getattr(self, bucket)
        setattr(self, bucket, current + elapsed_ms(since))
        self._mirror_bucket(bucket)

    def add_ms(self, bucket: str, ms: int) -> None:
        if bucket in WORKER_BUCKETS:
            setattr(self, bucket, getattr(self, bucket) + clamp_nonneg(ms))
            self._mirror_bucket(bucket)

    def _mirror_bucket(self, bucket: str) -> None:
        from common.telemetry import record_timing_bucket_on_current_span

        record_timing_bucket_on_current_span(
            section="worker",
            bucket=bucket,
            ms=getattr(self, bucket),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            key: clamp_nonneg(getattr(self, key))
            for key in WORKER_BUCKETS
            if getattr(self, key) > 0
        }

    def to_wire(self) -> dict[str, Any]:
        buckets = self.to_dict()
        return {"worker": buckets} if buckets else {}


@dataclass
class EngineTimingAccumulator:
    when_cel_ms: int = 0
    schedule_ms: int = 0
    policy_ms: int = 0
    dispatch_to_ingest_ms: int = 0
    _marks: dict[str, float] = field(default_factory=dict)
    _dispatch_anchor: float | None = None

    def start(self, label: str) -> None:
        self._marks[label] = time.perf_counter()

    def stop(self, label: str, *, bucket: str) -> None:
        since = self._marks.pop(label, None)
        if since is None or bucket not in ENGINE_BUCKETS:
            return
        current = getattr(self, bucket)
        setattr(self, bucket, current + elapsed_ms(since))
        self._mirror_bucket(bucket)

    def add_ms(self, bucket: str, ms: int) -> None:
        if bucket in ENGINE_BUCKETS:
            setattr(self, bucket, getattr(self, bucket) + clamp_nonneg(ms))
            self._mirror_bucket(bucket)

    def _mirror_bucket(self, bucket: str) -> None:
        from common.telemetry import record_timing_bucket_on_current_span

        record_timing_bucket_on_current_span(
            section="engine",
            bucket=bucket,
            ms=getattr(self, bucket),
        )

    def set_dispatch_anchor(self, anchor: float | None = None) -> None:
        self._dispatch_anchor = time.perf_counter() if anchor is None else anchor

    def record_dispatch_to_ingest(
        self,
        *,
        pending: dict[str, Any] | None,
        outbox_created_at: datetime | None = None,
    ) -> None:
        ms = compute_dispatch_to_ingest_ms(
            anchor=dispatch_anchor_from_pending(pending) or self._dispatch_anchor,
            outbox_created_at=outbox_created_at,
        )
        if ms is not None:
            self.dispatch_to_ingest_ms = ms

    def to_dict(self) -> dict[str, int]:
        return {
            key: clamp_nonneg(getattr(self, key))
            for key in ENGINE_BUCKETS
            if getattr(self, key) > 0
        }

    def to_pending(self) -> dict[str, Any]:
        return pending_engine_payload(self.to_dict(), dispatch_anchor=self._dispatch_anchor)
