---
sidebar_position: 6
pagination_prev: guides/manifests/mcp-and-tools
pagination_next: guides/manifests/policies
---

# Conditional branching (`when.cel`)

`when.cel` is an optional [CEL](https://cel.dev/) expression on a saga step. Before Warden schedules a step to run, it evaluates this expression. If the result is **true**, the step runs normally. If **false**, Warden skips the step (`SKIPPED`) and moves on to the **next forward step** in your manifest — the next blueprint step by `order_index`, not a compensation undo row. Use it for optional paths and guards on prior output or tool facts — not for blocking a commit after arguments are resolved (that is a [Policy](policies.md) gate).

End-to-end example: [GitHub MCP demo](../../getting-started/demo-github-mcp.md) skips `post-comment` when the repo has no open issues.

## Attaching to a saga step

Add a `when` block with a `cel` string on any forward step:

```yaml
  - id: post-comment
    kind: commit
    worker: github-demo-worker
    worker_version: "1.0.0"
    when:
      cel: "has(steps.triage.facts.triage_metrics) && steps.triage.facts.triage_metrics.total_count > 0"
    tools:
      allow:
        - name: add_issue_comment
```

| Result | What happens |
|--------|----------------|
| No `when` block | Step always runs when reached in manifest order |
| `true` | Step scheduled (worker or commit tool runs) |
| `false` | Step `SKIPPED`; engine advances to the next blueprint step by `order_index` |
| Expression error at runtime | Step `FAILED` with `WHEN_EVALUATION_FAILED` in status; saga may compensate |

Invalid `when.cel` syntax is **caught when you deploy the saga** — deploy fails before any instance runs.

## CEL evaluation context

At schedule time the engine exposes these top-level names in `when.cel`:

| Name | Contents |
|------|----------|
| `input` | Saga start payload (`context.input`) |
| `steps` | Full `context.steps` map — prior step `output.data`, `facts`, and pre-initialized empty buckets for steps not yet run |
| `saga` | `trace_id`, `namespace`, `status` |
| `step` | Blueprint step being scheduled: `id` (manifest `id` — CLI `--step-id`, keys under `steps`), `name` (manifest `name` — display label; often differs from `id`), `kind`, `order_index` |

`when.cel` does **not** receive resolved `with` arguments for the current step, policy `phase`, or `tool.name` — those belong to [policy CEL](policies.md#cel-evaluation-context). To gate a commit on values from an earlier step, read them from `steps.<prior_id>.output.data` or `steps.<prior_id>.facts.<into>.<field>`, or bind them in the prior step's output and reference them here.

### Reading prior steps

Only **prior** steps in forward order are meaningful. At saga start every step id is pre-initialized with `{output: {data: {}}, facts: {}}`. Paths like `steps.triage.output.data.summary` always resolve — to empty or null until that step completes, not an evaluation error. Optional **`facts`** buckets are different: if the extractor's tool never ran, the `into` bucket may be absent — use `has(steps.<id>.facts.<into>)` so a missing bucket becomes **false** (skip) instead of `WHEN_EVALUATION_FAILED`.

### Tool facts and naming

Manifest `facts.fields` keys (e.g. `total_count`) become saga-context names. JSONPath values (e.g. `"$.totalCount"`) read **raw MCP tool JSON**. CEL must use extracted names:

```text
steps.triage.facts.triage_metrics.total_count   ✓  (saga context)
$.totalCount                                     ✗  (raw tool JSON — not in CEL binding)
```

Walkthrough: [Saga manifests → Tool facts](saga-manifests.md#tool-facts-facts). Defensive patterns: [Lifecycle → Step `SKIPPED`](../../concepts/lifecycle.md#step-skipped).

## `when.cel` vs policy

Both use CEL, but they are different gates:

| | `when.cel` on a step | `policy:` on a step |
|--|----------------------|---------------------|
| Question | Should this step run at all? | May this step proceed past the gate? |
| Evaluated when | Before scheduling | `after_reason` or `before_commit` |
| If false | Step `SKIPPED`; saga advances to next blueprint step by `order_index` | Step `FAILED`; compensation if configured |
| `steps` in binding? | Yes — full `context.steps` | No — exposes `phase`, `input`, `arguments`, `output`, `saga`, `step`, `worker`, and `tool` only ([Policies → CEL evaluation context](policies.md#cel-evaluation-context)); no `steps` |

Policy CEL never receives `steps.*`. Gate on prior-step values via `with` into `arguments` (commit) or the current reason step's `output` (`after_reason`). Full policy reference: [Policies](policies.md).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Step `FAILED`; `WHEN_EVALUATION_FAILED` in `error_details` | CEL referenced a path that does not exist or wrong type at runtime | Use `has()` for optional `facts` buckets; verify field names match manifest `facts.fields` keys, not raw tool JSON |
| Step `SKIPPED` unexpectedly | Expression returned `false` | If your step is getting SKIPPED out of nowhere, your expression is likely returning false because of a missing piece of data. This usually happens if a prior step didn't output what you expected, a field name is misspelled, or you forgot to wrap an optional field in `has()`. Run `warden show step` to peek at what's actually sitting in context, and double-check your property paths against your manifest — see [Tool facts](saga-manifests.md#tool-facts-facts) for naming. |
| Deploy rejected on `when.cel` | Syntax or compile error | Fix the CEL expression and redeploy; same compile path as policy CEL |

```bash
warden list steps --trace-id <TRACE_ID> --errors
warden show step <TRACE_ID> --step-id <STEP_ID>
```

## What's next

Next up: [Policies](policies.md) — author deterministic runtime guardrails that validate model outputs and incoming tool parameters.

## Related

- [Saga manifests](saga-manifests.md) — step kinds, `with` bindings, tool facts
- [Lifecycle](../../concepts/lifecycle.md) — `SKIPPED` vs terminal cleanup
- [Terminology → Conditional branching](../../concepts/terminology.md#conditional-branching-whencel)
- [GitHub MCP demo](../../getting-started/demo-github-mcp.md) — conditional skip when `total_count == 0`
