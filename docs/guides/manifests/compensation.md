---
sidebar_position: 8
pagination_prev: guides/manifests/policies
pagination_next: guides/observability
---

# Compensation

If a saga fails halfway through, Warden rolls back completed steps in reverse order (last completed first). For each step that declared an undo path, the engine creates a child compensation row and dispatches one deterministic MCP tool call — the same shape as a commit step.

Reason and commit steps share that contract. On a mutating **reason** step, declare a single-tool net-effect undo (reset sandbox, cancel payment, …). Omit `compensation:` on read-only reason steps.

## Declaring compensation

Add a `compensation` field on the forward **step** definition. The value is a **path relative to `COMPENSATIONS_ROOT`** (alongside `PROMPTS_ROOT`, `POLICIES_ROOT`, and `SCHEMAS_ROOT`).

With the repo defaults (`COMPENSATIONS_ROOT=./config/compensations`), a file at `config/compensations/disburse_undo.yaml` is referenced as `disburse_undo.yaml`:

```yaml
# step catalog
kind: step
name: disburse
version: "1.0.0"
step_kind: reason
worker: payments-worker
worker_version: "1.0.0"
prompt: disburse.j2
compensation: disburse_undo.yaml
inputs: {}
```

```yaml
# saga composition
steps:
  - id: disburse
    use: disburse
    version: "1.0.0"
    with: {}
```

The compensation file lists the worker, bindings, and **exactly one** tool:

```yaml
worker: payments-worker
worker_version: "1.0.0"
with:
  payment_id:
    from: "$.steps.disburse.output.data.payment_id"
tools:
  allow:
    - name: cancel_payment
```

Supported fields: `worker`, `worker_version`, `with`, `tools` (exactly one allow entry), and optional `resources`. Deploy rejects any other keys and rejects an empty or multi-entry allowlist.

`worker_version` must match a deployed worker row in the saga's namespace. The worker invokes that MCP tool once with the resolved `with` arguments — no LLM.

Warden validates compensation YAML when you deploy a step (and again when a saga link-checks that step). At saga **start**, the engine resolves the artifact and freezes the compensation block onto each forward step row in the instance. Disk changes after a saga starts don't affect **running sagas**.

## Which steps get undone

When a forward step fails, Warden walks completed steps in reverse (last first). Where that walk **starts** depends on how the step failed:

- **Known failure** (policy deny, tool error, validation, HITL reject, …): undo starts at the last *completed* step **before** the one that failed.
- **Uncertain failure** (`TIMED_OUT` or worker crash): undo **includes the failing step** — Warden can't be sure what ran or what side effects landed.

Steps **without** a `compensation:` field are **skipped** during the walk — no undo child row, no worker call. Warden won't guess an undo path from the forward worker; you must declare compensation on each step that needs one.

If nothing is in scope to undo (usually a failure on the first step with nothing behind it), the saga goes straight to `FAILED` without entering `COMPENSATING`. If every in-scope step is skipped because none declare compensation, the saga can still enter `COMPENSATING` and finish as `COMPENSATED` in the same transaction. While `COMPENSATING`, the saga stays there until all scheduled undos succeed (`COMPENSATED`) or any undo fails (`FAILED`). See [Lifecycle → Compensation](../../concepts/lifecycle.md#compensation) for the state map.

Forward steps that are still `IN_PROGRESS` with declared compensation stay eligible for undo (partial-effect safety after a crash or stale claim). `PENDING` or `SKIPPED` forward steps are skipped even when compensation is declared.

## Execution metadata (`_compensation`)

Right before Warden figures out the arguments for your cleanup step, it injects a `_compensation` block into saga context for JSONPath resolution. This data is tied to the original forward step you're rolling back — useful when your undo logic needs span IDs or idempotency keys from that run:

| Field | Meaning |
|-------|---------|
| `blind_cleanup` | Forward step has no usable output (timeout or crash before output was written). |
| `dirty_failure` | Forward step is `TIMED_OUT` or `SYSTEM_CRASH`. |
| `has_forward_output` | Structured output exists for JSONPath. |
| `idempotency_key` | Command key (`comp-{trace}-{undo_span}`). |
| `undo_span_id` / `forward_span_id` | Child and forward span IDs. |

Missing JSONPath targets resolve to `null` — they won't crash the engine. When forward output might be missing (timeout or crash), design `with` bindings around `$.input`, literals, or optional fields.

## Tool idempotency

Your cleanup workers talk to external APIs over the network, so your undo logic needs to be idempotent. Warden tries to schedule each compensation step once, but network blips, worker drops, or queue timeouts can cause the same cleanup command to run twice.

Warden injects `warden_idempotency_key` into every compensation tool call's MCP arguments. Pass it through to your undo API as a deduplication token so reruns are safe.

## Worker snapshot

Warden freezes the worker **version** on the compensation command when it schedules the undo. The worker checks that version at execute time so a redeployed worker definition cannot silently change undo identity mid-rollback.

## Failure modes

| Event | Saga outcome |
|-------|-------------|
| `STEP_COMPENSATED` | LIFO continues: previous step compensation, or `SAGA_COMPENSATED` if done. |
| `COMPENSATION_FAILED` | `SAGA_FAILED` — hard stop. |
| Reaper `COMPENSATION_TIMEOUT` | Same as `COMPENSATION_FAILED` (enterprise governance worker). |
| Compensation schedule/build error | `SAGA_FAILED` — saga does not remain stuck in `COMPENSATING` without an outbox command. |

## Stuck `COMPENSATING`

| Symptom | Likely cause |
|---------|------------|
| Saga `COMPENSATING`, compensation row `COMPENSATING`, no `STEP_COMPENSATED` | Worker crash, outbox consumer marked command `FAILED`, or slow compensation. |
| Same + outbox `EXECUTE_COMPENSATION` `FAILED` | Handler exception; message won't auto-retry. |

If the compensation is safe to retry:

1. Wait for automatic claim + outbox reap (see [Configuration — recovery timeouts](../../getting-started/configuration.md#recovery-timeouts)), or fix the environment and run **`warden saga retry-compensation TRACE_ID STEP_SPAN_ID`**.
2. Confirm the external undo is idempotent via `warden_idempotency_key`.
3. Use `--force` only when a live worker may still hold the claim (same cautions as forward-step retry).

Do not schedule a second compensation child row for the same forward span. The engine deduplicates compensation rows that are already running or finished.

## Execution timing on undo rows

Compensation undo rows use the same `execution_timing` column as forward steps. Timing is stored on the **child** row (`compensates_span_id` set), not the forward row.

Worker buckets: `worker_init_ms`, `setup_ms`, `tool_ms`; `llm_ms` is `0`. Engine buckets: `schedule_ms` (child create + `EXECUTE_COMPENSATION` emit) and `dispatch_to_ingest_ms`. Inspect via `GET /v1/sagas/steps` or SQL filtered on `compensates_span_id IS NOT NULL` — see [Observability](../observability.md).

## What's next

Next up: [Observability](../observability.md) — inspect runs in Postgres and Jaeger (`execution_timing`, trace correlation). Then [CLI overview](../cli/overview.md) for day-to-day operator commands.

## Related

- [Architecture](../../advanced/architecture.md)
- [Saga recovery](../cli/saga-recovery.md)
