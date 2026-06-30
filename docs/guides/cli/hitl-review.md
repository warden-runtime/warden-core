---
sidebar_position: 4
pagination_prev: guides/cli/start-and-monitor
pagination_next: guides/cli/saga-recovery
---

# HITL review

When a step has `hitl: true` and passes policy gates, the engine pauses the saga at `AWAITING_HUMAN`. The saga does not continue until an operator approves, rejects, or requests a retry with guidance.

The pause is stored in Postgres — **no worker sits idle** while you review. Workers stay available for other steps and sagas; only the saga row reflects the hold.

When you are ready, run one of the `warden review` commands below. The CLI **accepts your decision and returns immediately** — it does not block until the saga moves on. The engine processes the decision asynchronously: approve resumes the step, reject triggers compensation.

To find holds and confirm outcomes, use `warden review list` before you act and `warden list sagas --trace-id <trace_id>` afterward. See [Start and monitor](start-and-monitor.md) for status values like `AWAITING_HUMAN` and `RUNNING`.

Each pending review needs two identifiers: `trace_id` (from `warden start saga`) and `step_span_id` (the specific step awaiting review). Both appear in `warden review list`.

## List pending reviews

```bash
warden review list
```

Example table columns: `namespace`, `trace_id`, `step_span_id`, `kind`, `subject`, `step_id`, `worker`.

| Flag | Description |
|------|-------------|
| `--namespace` | Filter by namespace |
| `--trace-id` | Filter to a specific saga instance |
| `--kind` | Filter by step kind (`reason` or `commit`) |

Add `--json` for machine-readable output. Each item includes `review_payload` — for commit steps that is the resolved tool arguments (`owner`, `repo`, `issue_number`, `body`, …); for reason steps it is the worker output held for review.

## Approve a step

```bash
warden review approve <trace_id> <step_span_id>
```

For reason steps, optionally pass edited output with `--output` or `--output-file` (JSON object). The saga resumes asynchronously after the decision is accepted.

## Reject a step

```bash
warden review reject <trace_id> <step_span_id>
```

Optionally pass a structured rejection reason with `--error` or `--error-file` (JSON object). Rejection triggers compensation for completed forward steps — see the [Compensation guide](../manifests/compensation.md) for what happens next.

## HITL retry (operator re-run)

```bash
warden review retry <trace_id> <step_span_id> --guidance "Clarify the summary before proceeding."
```

**HITL retry** applies only while the step is `AWAITING_HUMAN`. It re-runs the agent with optional operator `--guidance`. It respects `hitl_max_retries` on the step manifest.

This is not the same as:

- **LLM automated backoff** — `WARDEN_LLM_RETRY_*` in [Configuration](../../getting-started/configuration.md) (transient provider errors during execution)
- **Saga restart** — `warden start saga` with a new or idempotent `trace_id`
- **Compensation re-run** — `warden saga retry-compensation` after a failed undo step ([Saga recovery](saga-recovery.md))

## What's next

If the saga is running normally again, continue monitoring with [Start and monitor](start-and-monitor.md). If a step stalls in `IN_PROGRESS` instead of waiting for review, that is operator recovery — see [Saga recovery](saga-recovery.md) and the [operator retry matrix](overview.md#operator-retry-matrix).

To configure HITL on a step, see [Saga manifests](../manifests/saga-manifests.md). HTTP equivalents: [HITL](../api/hitl.md).
