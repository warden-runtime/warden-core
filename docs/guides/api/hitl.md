---
sidebar_position: 4
pagination_prev: guides/api/start-and-monitor
pagination_next: guides/api/recovery
---

# HITL

When a step has `hitl: true` and passes policy gates, the engine pauses the saga at **`AWAITING_HUMAN`**. The saga does not continue until an operator approves, rejects, or requests a retry with guidance.

The pause is stored in Postgres — **no worker sits idle** while you review. Workers stay available for other steps and sagas; only the saga row reflects the hold.

When you submit a decision, the API **accepts it and returns immediately** (**202**) — it does not block until the saga moves on. The engine processes the decision asynchronously: approve resumes the step, reject triggers compensation.

To find holds and confirm outcomes, use `GET /v1/sagas/pending-review` before you act and `GET /v1/sagas?trace_id=…` afterward. See [Start and monitor](start-and-monitor.md) for status values like `AWAITING_HUMAN` and `RUNNING`.

Each pending review needs two identifiers: `trace_id` (from saga start) and `step_span_id` (the specific step awaiting review). Both appear in the pending-review list.

This page covers listing pending reviews, submitting approve/reject decisions, and HITL retry. For stuck `IN_PROGRESS` steps — not HITL holds — see [Recovery](recovery.md) and the [operator retry matrix](overview.md#operator-retry-matrix).

:::info[Reason vs commit steps]
A **reason** step is where the agent thinks — structured LLM output (and optional MCP reads/tools). A **commit** step is where the agent acts — exactly one governed MCP tool call with resolved arguments. HITL pauses before that output merges (reason) or before the tool fires (commit).
:::

## List pending reviews

```bash
curl -sS "$ENGINE_URL/v1/sagas/pending-review"
```

Optional filters:

| Query param | Description |
|-------------|-------------|
| `trace_id` | Single saga instance |
| `namespace` | Namespace filter |
| `kind` | `reason` or `commit` |

Example item fields: `saga_trace_id`, `step_span_id`, `step_kind`, `review_subject`, `review_payload`.

### What `review_subject` means

`review_payload` holds the material under review. `review_subject` tells you **which shape** it is:

| `review_subject` | Step kind | What you are reviewing |
|------------------|-----------|------------------------|
| `output` | **reason** | The agent's completed reason-step output — held before merging into saga context |
| `arguments` | **commit** | The resolved MCP tool arguments the commit step **would** execute — the commit has not run yet |

Use `review_payload` as the primary input for your review UI. When you **approve** a **reason** step, you can optionally pass a fresh `output` object in the request body to edit the result before it merges into saga context. **Commit** steps proceed with the reviewed arguments unless you reject or retry.

Scoped to one saga:

```bash
curl -sS "$ENGINE_URL/v1/sagas/pending-review?trace_id=$TRACE_ID"
```

You can also poll saga status: `GET /v1/sagas?trace_id=$TRACE_ID` → `status: "AWAITING_HUMAN"`.

## Submit a decision (canonical)

For control-plane integrations, use the unified **`POST .../decision`** endpoint. It is the same route the CLI calls for approve and reject:

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/decision" \
  -H "Content-Type: application/json" \
  -d '{"decision": "APPROVE", "output": {}}'
```

| Field | Description |
|-------|-------------|
| `decision` | `APPROVE` or `REJECT` |
| `output` | Optional override on approve (reason steps — approve-with-edit) |
| `error_details` | Optional structured rejection reason on reject |

Returns **202** — the engine enqueues the decision and processes it asynchronously.

## Approve (shorthand)

Same behavior as `decision` with `APPROVE`:

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/approve" \
  -H "Content-Type: application/json" \
  -d '{"output": {}}'
```

Optional body `output` overrides reason-step output on approve-with-edit. Empty body is valid.

## Reject (shorthand)

Same behavior as `decision` with `REJECT`:

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/reject" \
  -H "Content-Type: application/json" \
  -d '{"error_details": {"reason": "operator rejected"}}'
```

Rejection triggers compensation for completed forward steps — [Compensation](../manifests/compensation.md).

## HITL retry (not operator recovery)

Re-run the held step with optional operator guidance:

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/$TRACE_ID/steps/$STEP_SPAN_ID/retry" \
  -H "Content-Type: application/json" \
  -d '{"guidance": "Clarify the summary before proceeding."}'
```

Applies **only** while status is `AWAITING_HUMAN`. Respects `hitl_max_retries` on the step manifest.

This is **not** `POST .../retry-step` (operator recovery for stuck `IN_PROGRESS` steps) — see [Recovery](recovery.md).

Also distinct from:

- **LLM automated backoff** — `WARDEN_LLM_RETRY_*` in [Configuration](../../getting-started/configuration.md) (transient provider errors during execution)
- **Saga restart** — `POST /v1/sagas/start` with a new or idempotent `trace_id`
- **Compensation re-run** — `POST .../retry-compensation` after a failed undo step ([Recovery](recovery.md))

## Policy failures vs HITL

Human review and automated policy gates are separate tracks. A policy denial never routes to HITL — the step goes **`FAILED`** immediately.

When policy fails, poll step rows for `error_details`:

- **Policy denied** — step → `FAILED` with a code such as `POLICY_REASON_DENIED`; the saga may move to `FAILED` or `COMPENSATING`.
- **Policy errored** — step → `FAILED` with `POLICY_EVALUATION_FAILED`.
- **Policy passed + `hitl: true`** — step and saga → `AWAITING_HUMAN`; list via `/pending-review` or poll endpoints.

:::tip[MCP execution order]
On **`react`** reason steps, MCP tools on the allowlist may already have run before an `after_reason` policy evaluates. For a single irreversible side effect with a hard gate, use a **commit** step — see [Policies](../manifests/policies.md).
:::

## What's next

After a decision, continue polling with [Start and monitor](start-and-monitor.md). If a step stalls in `IN_PROGRESS` instead of waiting for review, that is operator recovery — [Recovery](recovery.md). CLI equivalent: [HITL review](../cli/hitl-review.md). Schema details: [API Reference](/docs/api/api-reference) — [Pending Review Steps](/docs/api/pending-review-steps-v-1-sagas-pending-review-get), [Decide Step](/docs/api/decide-step-v-1-sagas-trace-id-steps-step-span-id-decision-post), and related HITL routes.
