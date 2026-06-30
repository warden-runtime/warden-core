---
sidebar_position: 5
pagination_prev: guides/api/hitl
pagination_next: advanced/testing
---

# Recovery

When a workflow stops making progress, the system provides several built-in safety nets to kick it back into gear before you ever have to resort to manual database intervention:

1. Wait for automatic claim + outbox reap ([recovery timeouts](../../getting-started/configuration.md#recovery-timeouts)).
2. Call the operator retry endpoints below (**202 Accepted** — async re-queue).
3. Break-glass SQL only when automation and operator commands fail — [Saga recovery (CLI)](../cli/saga-recovery.md#diagnostics).

This page covers forward step retry and compensation retry over HTTP. HITL retry (`POST .../retry`) is a different path — see [HITL](hitl.md) and the [operator retry matrix](overview.md#operator-retry-matrix).

:::info[Enterprise]
The open kernel already reaps **stale worker claims** and **orphaned outbox rows** (see [Saga recovery — Automatic recovery](../cli/saga-recovery.md#automatic-recovery)). It does **not** automatically enforce manifest `timeout_seconds` on a live step, expire `AWAITING_HUMAN` reviews on a schedule, or fail undo rows stuck in `COMPENSATING` past their timeout.

For step timeouts, HITL SLA enforcement, and compensation timeout handling, see [Open Core vs Enterprise](../../getting-started/open-core-vs-enterprise.md).
:::

## Retry a forward step

If a forward-moving step gets locked up in **`IN_PROGRESS`** while the saga is still **`RUNNING`**, you can tell the engine to re-enqueue it.

Recovery settings are split between **URL query parameters** and a **JSON request body** — do not pass body fields (like `recovery_token`) as query strings.

### Query parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `namespace` | No (defaults to `default`) | Namespace of the target saga instance. |

Path segments `{trace_id}` and `{step_span_id}` identify the saga and step (see [Discover `step_span_id`](#discover-step_span_id)).

### JSON body fields

| Field | Type | Description |
|-------|------|-------------|
| `force` | Boolean | Release an active worker claim and trigger immediate redelivery. |
| `allow_destructive` | Boolean | Required with `force` on **commit** steps — acknowledges duplicate side-effect risk. |
| `recovery_token` | String | Optional idempotency token (8–128 chars); duplicate requests with the same token and parameters return the original **202** body without re-enqueueing. |
| `reason` | String | Optional operator note (enterprise audit hooks when plugins enabled). |

By default, if an active worker process still holds a valid, **non-stale** claim on a step, the engine returns **202 Accepted** with `"status": "claim_active"`. This guardrail prevents accidentally double-delivering tasks while the original worker is still trying to finish. Wait for automatic reap or resend with `"force": true`.

:::warning[Commit steps and `allow_destructive`]
A **commit** step executes real MCP side effects. `"force": true` without `"allow_destructive": true` on a commit step returns **409**. Only set both when you accept the risk of the external action running twice.
:::

Minimal retry (empty body):

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/retry-step?namespace=default" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Force retry with an idempotency token (safe to retry the same body after a network timeout):

```bash
RECOVERY_TOKEN="$(uuidgen | tr -d '-')"
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/retry-step?namespace=default" \
  -H "Content-Type: application/json" \
  -d "{\"force\": true, \"recovery_token\": \"$RECOVERY_TOKEN\"}"
```

Force retry without an idempotency token:

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/retry-step?namespace=default" \
  -H "Content-Type: application/json" \
  -d '{"force": true}'
```

Commit step with force:

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/retry-step?namespace=default" \
  -H "Content-Type: application/json" \
  -d '{"force": true, "allow_destructive": true}'
```

### Responses

| Code / body | Meaning |
|-------------|---------|
| **202** + `"status": "requeued"` or `"scheduled"` | Recovery accepted; engine re-queues work |
| **202** + `"status": "claim_active"` | Active worker still holds a non-stale claim — wait for reap or resend with `"force": true` |
| **404** | Saga or step not found |
| **409** | Precondition conflict (wrong saga/step status, `force` on commit without `allow_destructive`, `recovery_token` reused with different parameters, etc.) |

CLI equivalent: `warden saga retry-step <trace_id> <step_span_id>`.

## Retry compensation

Re-run a failed or stuck **compensation** undo step on a saga in **`COMPENSATING`** or **`FAILED`**. The compensation step may be **`FAILED`**, **`IN_PROGRESS`**, or **`COMPENSATING`**.

:::danger[Targeting the right span ID]
When retrying compensation, pass the `step_span_id` of the **compensation step itself** — the undo row with a non-empty `compensates_span_id` in the step list. Passing the original forward step's ID returns **409** (FSM precondition conflict).
:::

Use the same query/body split as `retry-step` (`namespace` in the query string; `force`, `recovery_token`, and `reason` in the JSON body). `allow_destructive` is accepted by the schema but applies to forward commit retries, not compensation.

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$COMPENSATION_STEP_SPAN_ID/retry-compensation?namespace=default" \
  -H "Content-Type: application/json" \
  -d '{}'
```

CLI equivalent: `warden saga retry-compensation <trace_id> <step_span_id>`.

## Not the same as HITL retry

See the full [operator retry matrix](overview.md#operator-retry-matrix) — this page covers forward and compensation recovery only.

Also distinct from **LLM automated backoff** (`WARDEN_LLM_RETRY_*` in [Configuration](../../getting-started/configuration.md)) and **saga restart** (`POST /v1/sagas/start` with a new trace).

## Discover `step_span_id`

Poll steps while diagnosing — compensation rows include `compensates_span_id`:

```bash
curl -sS "$ENGINE_URL/v1/sagas/steps?trace_id=$TRACE_ID"
```

Read `step_span_id`, `step_id`, `status`, and `compensates_span_id` from each item in the JSON response.

## What's next

You have the operator recovery endpoints for stuck forward and compensation steps. For SQL diagnostics and automatic reap details, see [Saga recovery (CLI)](../cli/saga-recovery.md). To poll before escalating, use [Start and monitor](start-and-monitor.md). Schema details: [API Reference](/docs/api/api-reference) — [Operator Retry Step](/docs/api/operator-retry-step-v-1-sagas-trace-id-steps-step-span-id-retry-step-post), [Operator Retry Compensation](/docs/api/operator-retry-compensation-v-1-sagas-trace-id-steps-step-span-id-retry-compensation-post).
