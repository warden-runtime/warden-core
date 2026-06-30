---
sidebar_position: 5
pagination_prev: guides/cli/hitl-review
pagination_next: guides/api/overview
---

# Saga recovery

When a workflow stops making progress, the system provides several built-in safety nets to kick it back into gear before you ever have to resort to manual database intervention:

1. **Wait for automatic recovery** — background loops reap stale worker claims and outbox rows stuck `IN_PROGRESS` (see [recovery timeouts](../../getting-started/configuration.md#recovery-timeouts)).
2. **Operator retry** — `warden saga retry-step` or `warden saga retry-compensation`.
3. **Break-glass SQL** — only when automation and operator commands are insufficient (see [Diagnostics](#diagnostics)).

Workflows usually stall for a few predictable reasons: a worker process crashes mid-task before reporting back, the outbox message queue gets backed up, or an undo/compensation step runs into an unhandled environmental error. The sections below cover operator retry commands first, then automatic recovery and SQL diagnostics.

:::info[Enterprise]
The open kernel already reaps **stale worker claims** and **orphaned outbox rows** (see [Automatic recovery](#automatic-recovery) below). It does **not** automatically enforce manifest `timeout_seconds` on a live step, expire `AWAITING_HUMAN` reviews on a schedule, or fail undo rows stuck in `COMPENSATING` past their timeout.

For those governance reapers — step timeouts, HITL SLA enforcement, and compensation timeout handling — see [Open Core vs Enterprise](../../getting-started/open-core-vs-enterprise.md).
:::

## Retry a forward step

Re-queue a stuck **forward** step on a **`RUNNING`** saga where the step is **`IN_PROGRESS`**.

By default, if an active worker process still holds a valid, **non-stale** claim on the step's command, the engine returns **`claim_active`**. This guardrail prevents accidentally double-delivering tasks while the original worker is still trying to finish. After automatic reap windows expire, a bare retry can succeed; otherwise pass `--force` to release the claim early (commit steps also need `--allow-destructive`).

```bash
warden saga retry-step <trace_id> <step_span_id>
```

| Flag | Description |
|------|-------------|
| `--namespace` | Saga namespace (default `default`) |
| `--force` | Release a non-stale worker claim blocking redelivery |
| `--allow-destructive` | Required with `--force` on **commit** steps (duplicate side-effect risk) |
| `--recovery-token` | Optional client idempotency token; duplicate CLI/HTTP calls with the same token and flags return the original **202** body |
| `--reason` | Optional operator note (enterprise audit hooks) |

Examples:

```bash
# Stuck reason step after crash or orphaned claim
warden saga retry-step abc123… span456…

# Release an active claim before the stale-claim timeout (reason steps)
warden saga retry-step abc123… span456… --force

# Commit step — both flags required
warden saga retry-step abc123… span456… --force --allow-destructive
```

HTTP equivalent: `POST /v1/sagas/{trace_id}/steps/{step_span_id}/retry-step` — see [Recovery](../api/recovery.md).

## Retry compensation

Re-run a failed or stalled **compensation** undo step.

```bash
warden saga retry-compensation <trace_id> <step_span_id>
```

:::danger[Targeting the right span ID]
When retrying compensation, pass the `span_id` of the **compensation step itself** — the undo row with a non-empty `compensates` value. Passing the original forward step's ID returns **409** (FSM precondition conflict).
:::

Saga must be **`COMPENSATING`** or **`FAILED`**; the compensation step may be **`FAILED`**, **`IN_PROGRESS`**, or **`COMPENSATING`**.

| Flag | Description |
|------|-------------|
| `--namespace` | Saga namespace (default `default`) |
| `--force` | Release a non-stale worker claim |
| `--recovery-token` | Optional client idempotency token; duplicate CLI/HTTP calls with the same token and flags return the original **202** body |
| `--reason` | Optional operator note |

After fixing the underlying tool or environment error:

```bash
warden saga retry-compensation TRACE_ID COMPENSATION_STEP_SPAN_ID
```

For LIFO unwind behavior and failure modes, see the [Compensation guide](../manifests/compensation.md).

## Not the same as HITL retry

See the full [operator retry matrix](overview.md#operator-retry-matrix) — this page covers forward and compensation recovery only.

Also distinct from **LLM automated backoff** (`WARDEN_LLM_RETRY_*` in [Configuration](../../getting-started/configuration.md)) and **saga restart** (`warden start saga` with a new trace).

## Automatic recovery

Two background maintenance loops run in the worker and engine processes:

| Loop | What it watches | Default threshold | Action |
|------|-----------------|-------------------|--------|
| Claim reap | Stale worker claims | `WORKER_STALE_CLAIM_SECONDS` (**1800** s) | Clears unfinished claims so commands can be redelivered |
| Outbox reap | Stale `IN_PROGRESS` outbox rows | `OUTBOX_STALE_IN_PROGRESS_SECONDS` (**1800** s) | Resets rows to `PENDING` for redelivery |

Tune these timeouts so they exceed worst-case LLM/MCP latency for your manifests. If you see frequent superseded-claim log lines within seconds of execution, the timeouts may be too aggressive.

## Diagnostics

Use the CLI first: `warden list sagas --trace-id …`, `warden list steps --trace-id …`. On the dev stack, Adminer at `http://127.0.0.1:8080` or raw SQL below.

### Step status and outbox backlog

```sql
-- Steps still in flight
SELECT namespace, saga_trace_id, span_id, step_id, status, started_at, end_time
FROM saga_step_instances
WHERE status IN ('IN_PROGRESS', 'AWAITING_HUMAN');

-- Undelivered outbox messages
SELECT destination_topic, event_type, status, count(*)
FROM outbox_events
WHERE status = 'PENDING'
GROUP BY destination_topic, event_type, status;
```

- **`IN_PROGRESS` with no worker activity** — worker may have crashed after claiming a command. Check worker logs for the saga `trace_id`. Compare against manifest `timeout_seconds`.
- **Growing `PENDING` rows on `worker-commands`** — workers are not consuming the outbox (down or underprovisioned).

### Compensation in progress

```sql
SELECT namespace, trace_id, status, started_at
FROM saga_instances
WHERE status = 'COMPENSATING';
```

Join to `saga_step_instances` on `saga_trace_id` to find compensation rows stuck in `IN_PROGRESS` or `FAILED`. Inspect worker logs for errors on the undo step's `span_id`.

:::warning[Do not force-fail via raw SQL]
Updating `saga_step_instances.status` directly (for example setting `FAILED` on an `IN_PROGRESS` row) does **not** run the engine failure lifecycle — no compensation dispatch. Prefer automatic reap + `warden saga retry-step`, then the outbox sideline below if needed.
:::

**Sideline a stuck `IN_PROGRESS` outbox row** (visibility only; does not repair the saga FSM):

```sql
-- Only when you have confirmed the consumer will not finish this row
UPDATE outbox_events
SET status = 'FAILED'
WHERE id = '<outbox_uuid>' AND status = 'IN_PROGRESS';
```

Re-driving the business action requires automatic reap, `warden saga retry-step`, or a new recovery command — not flipping saga rows directly.

## What's next

You have the recovery ladder for stuck forward and compensation steps. The [API guides](../api/overview.md) cover the same operator endpoints with curl — start with [Recovery](../api/recovery.md). For day-to-day monitoring before escalation, keep [Start and monitor](start-and-monitor.md) and [Observability](../observability.md) handy.
