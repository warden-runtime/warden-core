---
title: "Demo: Observe Execution Timing"
sidebar_position: 4
sidebar_label: "Demo: Observe Execution Timing"
pagination_prev: getting-started/demo-mock-llm-and-mcp
pagination_next: getting-started/demo-quickstart
---

# Demo: Observe Execution Timing

The previous demo leaves you with a completed saga and a known-good `trace_id`. Here you read millisecond-level timing buckets from Postgres — the same data Warden surfaces in the CLI and API when a step finishes. You do not redeploy or restart the stack; reuse the `trace_id` from [Demo: Mock LLM and MCP](demo-mock-llm-and-mcp.md).

Mock runs are near-instant; production workflows are not. These buckets show where time goes before you add policy and human gates in the [GitHub MCP demo](demo-github-mcp.md).

## Prerequisites

- Completed [Demo: Mock LLM and MCP](demo-mock-llm-and-mcp.md).
- Step `greet` is `COMPLETED` for your `trace_id` (the saga has two steps; timing on `greet` is enough for this page).

If you skipped the manifests during the mock demo, open `config/saga.mock-mcp.yaml` and the prompt files from [Demo: Mock LLM and MCP → What you'll use](demo-mock-llm-and-mcp.md#what-youll-use) — you'll map each timing bucket to a step you already ran.

## Read the timing JSON

When a step finishes, Warden aggregates worker and engine metrics and writes them to the `execution_timing` column on that step's Postgres row. Read them with `show step` (the CLI prints a `timing` block; add `--json` for the raw object):

```bash
warden show step <YOUR_TRACE_ID> --step-id greet --namespace default
```

**Example shape** on the mock stack (values vary by machine, load, and poll timing):

```json
{
  "worker": {
    "hydration_ms": 5,
    "setup_ms": 4
  },
  "engine": {
    "schedule_ms": 16,
    "dispatch_to_ingest_ms": 850
  }
}
```

Mock runs often omit `llm_ms` and `tool_ms` — sub-millisecond work is dropped when the bucket is zero.

| Domain | Key | What it measures |
|--------|-----|------------------|
| Worker | `hydration_ms` | Worker manifest load and prompt render |
| Worker | `setup_ms` | Provider client init and tool binding |
| Worker | `llm_ms` | Wall-clock on model responses (`react`: across ReAct turns; `simple`: one call) |
| Worker | `tool_ms` | MCP tool execution |
| Engine | `schedule_ms` | FSM evaluation, locks, step dispatch |
| Engine | `dispatch_to_ingest_ms` | End-to-end step wall-clock from worker dispatch to result ingest |

Full reference: [Observability → Execution timing](../guides/observability.md#execution-timing).

### Wall-clock envelope

:::note

**Start here for “how long did this step take?”** — `dispatch_to_ingest_ms` is the single end-to-end number for the step: engine dispatch → worker runs → engine receives the result. `worker.llm_ms` and `worker.tool_ms` break out work **inside** the worker during that window; they overlap this bucket, so do not add them together.

On reason steps with live inference, the envelope grows with LLM + tool time plus poll jitter — expected, not double-counting. Tune with the worker buckets; use the envelope for total step latency.

:::

### Outbox polling on the dev stack

:::note

The default Postgres outbox consumer polls every second between reads. Even when the worker finishes in a few milliseconds, `dispatch_to_ingest_ms` still counts the **whole step** — worker claim and execution, the worker writing its result to the outbox, and the engine picking that result up. On fast mock or commit steps that total often lands between a few hundred milliseconds and about two seconds because of poll jitter, not because the worker was slow.

:::

## When timing looks wrong

| Observation | Likely cause | What to do |
|-------------|--------------|------------|
| `dispatch_to_ingest_ms` a few seconds on mock/commit steps; `llm_ms` absent or ~0 | Poll jitter + empty envelope (normal floor) | Expected on dev stack — see [outbox note](#outbox-polling-on-the-dev-stack). |
| `dispatch_to_ingest_ms` seconds to tens of seconds on reason steps; `llm_ms` / `tool_ms` explain most of it | Envelope includes live inference and MCP (normal) | Expected on [Quickstart](demo-quickstart.md) and [GitHub MCP](demo-github-mcp.md). The envelope roughly tracks worker buckets plus poll jitter — 5s, 10s, and 15s are all normal on heavy ReAct steps. Tune using `worker.llm_ms` and `worker.tool_ms`. |
| `dispatch_to_ingest_ms` high but **out of proportion** to worker buckets (e.g. 30s+ envelope while `llm_ms` / `tool_ms` are absent or ~0), or step stuck `IN_PROGRESS` | Worker down, outbox not consumed, or hung step — not slow inference | `make doctor`; worker/engine logs; see [Troubleshooting → Local stack diagnostics](troubleshooting.md#local-stack-diagnostics). |
| `llm_ms` absent or ~0 on a live-worker run | Mock provider still deployed, or sub-ms work dropped | Redeploy live worker from Quickstart; re-read timing. |
| `llm_ms` in seconds | Live inference (normal) | Preview — you'll tune this in GitHub MCP. |
| `tool_ms` high, `llm_ms` normal | MCP / subprocess bound | Audit tool servers, cold starts, network (GitHub MCP demo). |
| `schedule_ms` spikes | Engine contention / FSM work | Rare on dev stack; check engine logs and concurrent sagas. |

## Operational takeaways

Two latency centers matter when you tune workflows — on the mock stack both are near-zero; you'll revisit them with live data:

1. **Model inference (`worker.llm_ms`)** — Near-instant here. With a live cloud or local model ([Quickstart](demo-quickstart.md)), expect **seconds**.
2. **Integration (`worker.tool_ms`)** — Time in MCP tools. Shows up on [GitHub MCP](demo-github-mcp.md) when ReAct calls remote tools.

Use this split to tell whether a slow saga is model-bound, tool-bound, or engine scheduling overhead (`engine.schedule_ms`). Prompt size, model choice, and context pruning matter once `llm_ms` is non-zero — you'll see that in later demos.

## Optional: Jaeger spans

<details>
<summary>Visual verification in Jaeger (dev stack only)</summary>

When you boot the stack with `make up`, the engine and worker stream OTLP traces to Jaeger ([Configuration → Default ports](configuration.md#default-ports-dev-stack)).

1. Open http://127.0.0.1:16686
2. Service: **`engine-node`** or **`worker-node`**
3. Tag filter: `saga.id=<YOUR_TRACE_ID>`
4. **Find Traces**

Filter on `saga.id`, not Jaeger's top-level trace ID. If you do not see data, check your filters right after the saga finishes — the dev stack flushes traces quickly and they roll out of view if you wait too long. The CLI `timing` JSON is the primary teaching artifact for this demo.

See [Observability → Warden correlation on spans](../guides/observability.md#warden-correlation-on-spans).

</details>

## What's next

Continue with [Demo: Quickstart](demo-quickstart.md) to swap the mock worker for live inference and watch `worker.llm_ms` jump to seconds. Then open [Demo: GitHub MCP](demo-github-mcp.md) — policy, HITL, and conditional steps on a real repo, and the reason Warden exists beyond a job queue with an LLM call.

## Related

- [Observability](../guides/observability.md) — Postgres, OTLP, timing reference
