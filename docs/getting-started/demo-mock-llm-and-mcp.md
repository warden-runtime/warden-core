---
title: "Demo: Mock LLM and MCP"
sidebar_position: 3
sidebar_label: "Demo: Mock LLM and MCP"
pagination_prev: getting-started/installation
pagination_next: getting-started/demo-observe-execution-timing
---

# Demo: Mock LLM and MCP

This is the first hands-on demo in getting started — no API keys required. You deploy two bundled manifests, start one saga with two steps, and watch the worker run a short reasoning loop on each step. When both steps finish, the saga is `COMPLETED` in Postgres.

You need a running stack from [Installation](installation.md) — `warden ping` should already succeed. No extra `.env` beyond what Installation set up.

This demo focuses on the core orchestration loop. Policy rules and human-in-the-loop review appear in the later [GitHub MCP](demo-github-mcp.md) demo.

## What you'll use

| Artifact | Path |
|----------|------|
| Worker | `config/worker.mock-mcp.yaml` — `mock-mcp-worker`, mock LLM provider |
| Saga | `config/saga.mock-mcp.yaml` — `greet` then `summarize` (reads prior step output) |
| Prompts | `config/prompts/mock-greet.j2`, `config/prompts/mock-summarize.j2` |
| Mock runtime | `workers/fixtures/mock_mcp_server.py`, `workers/llm/mock.py` |

Before you deploy, open the files in the table — worker provider, step order, prompts, and tool allowlists make more sense once you've read the YAML.

## Before you start

1. Finish [Installation](installation.md) — `warden ping` returns healthy.
2. No `OPENAI_API_KEY` or other cloud credentials needed.

## Walkthrough \{#manual-cli-walkthrough\}

From the repo root with `ENGINE_URL` set. Run commands with `source .venv/bin/activate` or prefix with `uv run` — see [Installation](installation.md).

### 1. Deploy the manifests

Deploy the worker first, then the saga. Warden rejects the saga deploy if the worker is not registered yet.

```bash
warden deploy -f config/worker.mock-mcp.yaml
warden deploy -f config/saga.mock-mcp.yaml
```

### 2. Start the saga

```bash
warden start saga -n mock-mcp-saga -v 0.1.0 --input '{"name":"Ada"}'
```

Copy the `trace_id` from the response. You will need it for the commands below and for the [next demo](demo-observe-execution-timing.md). The input name is passed into the first step's prompt; any value works for this mock run.

### 3. Check saga status

```bash
warden list sagas --trace-id <YOUR_TRACE_ID> --namespace default
```

You'll watch the saga move `PENDING` → `RUNNING` → `COMPLETED`. On the mock stack that transition usually finishes before you can type the next command, so seeing `COMPLETED` on your first `list sagas` is expected — not a sign anything was skipped.

### 4. Check step progress

```bash
warden list steps --trace-id <YOUR_TRACE_ID> --namespace default
```

You should see two rows: `greet`, then `summarize`. On the happy path, each step moves `PENDING` → `IN_PROGRESS` → `COMPLETED`. If a step errors, Warden marks it `FAILED` — inspect it with `show step` (next section).

`list steps` is a **status index**: order, lifecycle state, timing buckets. It does not include resolved inputs, outputs, or prompt references. Add `--json` when you want machine-readable timing or `error_details` on a failed step. For polling instead of one-shot listing, add `--watch` ([Start and monitor](../guides/cli/start-and-monitor.md)).

### 5. Inspect step data

```bash
warden show step <YOUR_TRACE_ID> --step-id greet --namespace default
warden show step <YOUR_TRACE_ID> --step-id summarize --namespace default
```

`show step` answers what actually ran: `resolved_arguments`, `output_payload`, and `prompt_ref`.

On **greet**, expect `resolved_arguments.name` from your saga input and `output_payload.data.greeting` from the mock worker. On **summarize**, expect `resolved_arguments.greeting` chained from the first step and `output_payload.data.summary`.

Use `--json` for scripting. If human output looks truncated (large ReAct payloads on other demos), add `--raw` or `--json` for the full blob.

Mock timing often omits `llm_ms` and `tool_ms` because those buckets are sub-millisecond and zero values are dropped. `dispatch_to_ingest_ms` usually dominates on the mock stack — see the [outbox polling note](demo-observe-execution-timing.md#outbox-polling-on-the-dev-stack) on the next demo page.

## What just happened

You ran the core loop end to end across two steps: Warden queued work on the outbox, the worker claimed each step, started a local MCP subprocess, and the mock LLM finished a ReAct turn per step. Step `summarize` consumed output from step `greet` before submitting its own result. The saga landed in `COMPLETED` in Postgres.

Warden uses this exact same transactional loop to run live production workflows. To see how Warden maps non-deterministic LLM outputs to deterministic Postgres state across reason and commit steps, read [Durable execution boundaries](../concepts/durable-execution.md). The [GitHub MCP demo](demo-github-mcp.md) layers policy and HITL on top after the next two demos.

## If something goes wrong \{#operational-diagnostics\}

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ENGINE_URL` / health check fail | Engine not reachable | [Troubleshooting → Stack and CLI](troubleshooting.md#stack-and-cli) |
| Saga `RUNNING` but a step stuck `IN_PROGRESS` | Worker down or slow claim | `make doctor`; worker logs |
| Tool not in allowlist | Tool name mismatch in saga | Allow `echo` in the step's `tools.allow` list |
| MCP subprocess spawn fail | Worker image missing the fixture | Rebuild: `docker compose build worker`; see [Local stack diagnostics](troubleshooting.md#local-stack-diagnostics) |
| `summarize` fails after `greet` completes | Stale saga definition (one-step version) | Redeploy `config/saga.mock-mcp.yaml` |

For `make doctor`, log dumps, and reset workflows, see [Troubleshooting](troubleshooting.md).

## What's next

Continue with [Demo: Observe Execution Timing](demo-observe-execution-timing.md) using the `trace_id` from this run.

## Related

- [Observability](../guides/observability.md) — execution timing reference
- [GitHub MCP demo](demo-github-mcp.md) — policy, HITL, external MCP
