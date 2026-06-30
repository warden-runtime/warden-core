---
sidebar_position: 3
pagination_prev: guides/cli/deploy-and-list
pagination_next: guides/cli/hitl-review
---

# Start and monitor

Starting a saga returns a `trace_id` immediately; workers execute steps asynchronously. From the CLI you start with `warden start saga`, then poll with `warden list sagas` and `warden list steps` until the instance reaches a terminal status or pauses for human review.

After you start a saga, **`trace_id`** is your handle for everything — a 32-character hex token, not the manifest name or version. Copy it from the start output and use it for list, review, and recovery commands.

## Start a saga

```bash
warden start saga -n <name> -v <version> [--namespace <namespace>]
```

Omit `--namespace` and the engine uses `default`. The CLI prints `trace_id` on success:

```bash
warden start saga -n minimal-saga -v 0.0.1 --namespace default
# SUCCESS      trace_id=7f3a9c2e1b4d8f0a6e5c3b2a1d9f8e7c
```

To pass input data into the saga's context:

```bash
# Inline JSON
warden start saga -n minimal-saga -v 0.0.1 --input '{"key": "value"}'

# From a file
warden start saga -n minimal-saga -v 0.0.1 --input-file ./input.json
```

When you start a saga, Warden stores your payload under the **`input`** key in saga context — not at the context root. Manifest bindings use JSONPath like `$.input.repo`; `when.cel` and policy CEL expose the same shape as top-level `input` (for example `input.owner`). Jinja prompt variables come from each step's resolved `with` map, not directly from the start payload. See [Saga manifests → Bindings](../manifests/saga-manifests.md#bindings-with).

Use `--idempotency-key` to prevent duplicate starts if you retry the same HTTP request. Keys are scoped to **`(namespace, idempotency_key)`** — the same key string in a different namespace starts a separate instance.

## List saga instances

```bash
warden list sagas
```

| Flag | Description |
|------|-------------|
| `--trace-id` | Single saga instance (32-char hex) |
| `--in-flight` | Non-terminal sagas: `PENDING`, `RUNNING`, `AWAITING_HUMAN`, `COMPENSATING` |
| `--failed` | Show only `FAILED` sagas |
| `--status <status>` | Filter by a specific status (repeatable) |
| `--namespace` | Filter by namespace |
| `--watch` | Poll and reprint until terminal (interactive terminal only) |
| `--interval` | Seconds between polls with `--watch` (default 0.5) |

Add `--json` for machine-readable output.

## List saga steps

```bash
warden list steps --trace-id <trace_id> [--namespace default]
```

| Flag | Description |
|------|-------------|
| `--trace-id` | Required — saga instance from `warden start saga` |
| `--status <status>` | Filter step rows (repeatable), e.g. `COMPLETED`, `IN_PROGRESS`, `FAILED` |
| `--namespace` | Optional guard; must match the saga instance row |
| `--watch` | Poll until every returned step is terminal (interactive terminal only) |
| `--interval` | Seconds between polls with `--watch` (default 0.5) |

Steps are ordered by `order_index`. Compensation rows show which forward step they undo in the `compensates` column.

Add `--json` for machine-readable output — use it to inspect `error_details` on failed steps.

## Saga status

| Status | Meaning |
|--------|---------|
| `PENDING` | Instance created; scheduling not yet complete |
| `RUNNING` | Actively executing steps |
| `AWAITING_HUMAN` | Paused at a step pending human review |
| `COMPENSATING` | A failure triggered compensation; unwinding completed steps |
| `COMPLETED` | All steps finished successfully |
| `FAILED` | The saga failed and could not compensate |
| `COMPENSATED` | Compensation ran to completion |

For compensation behavior on `FAILED` / `COMPENSATING`, see the [Compensation guide](../manifests/compensation.md).

## Per-step detail

`warden list steps --trace-id <trace_id>` is the status index (lifecycle and timing). For resolved inputs, outputs, and prompt references:

```bash
warden show step <trace_id> --step-id <step_id> [--namespace default]
# or: warden show step <trace_id> <step_span_id>
```

Add `--json` for machine-readable output, or `--raw` when human output truncates a large payload. Failed steps surface `error_details` before payloads.

For raw SQL or Adminer on the dev stack, see [Observability](../observability.md).

## What's next

If a saga reaches `AWAITING_HUMAN`, an operator must approve, reject, or retry the held step: [HITL review](hitl-review.md).

If a step stays `IN_PROGRESS` while the worker is healthy, wait for [recovery timeouts](../../getting-started/configuration.md#recovery-timeouts) first, then escalate:

```bash
warden saga retry-step <trace_id> <step_span_id>
```

If the CLI reports **`claim_active`**, a worker still holds a non-stale claim on the command — retry with `--force` (commit steps also need `--allow-destructive`). Full ladder: [Saga recovery](saga-recovery.md).
