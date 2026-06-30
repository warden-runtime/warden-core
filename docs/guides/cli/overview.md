---
sidebar_position: 1
pagination_prev: guides/observability
pagination_next: guides/cli/deploy-and-list
---

# CLI overview

The `warden` CLI is a thin HTTP client: every command talks to the engine at `ENGINE_URL`. There is no direct Postgres access from the CLI. This section walks through install, health check, and the command map in sidebar order — deploy and list, start and monitor, HITL review, then saga recovery.

If you prefer curl or are building your own control plane, the [API guides](../api/overview.md) mirror the same flows. For a hands-on runbook that uses every major command, see [Demo: GitHub MCP](../../getting-started/demo-github-mcp.md) in Getting Started.

Set the engine URL once before any command:

```bash
export ENGINE_URL=http://127.0.0.1:8000
```

If `ENGINE_URL` is unset, the CLI exits with an explicit error before making any request.

## Install

Clone the repository, then install CLI dependencies:

```bash
git clone https://github.com/warden-runtime/warden-core.git
cd warden-core
make sync-dev
# or: uv sync --extra dev --extra engine --extra worker --extra cli
```

That creates `.venv/` and installs the `warden` entry point at `.venv/bin/warden`. The CLI is not on your global `PATH` — activate the virtualenv or use `uv run`:

```bash
source .venv/bin/activate   # then: warden ping, warden deploy, …
```

```bash
uv run warden ping          # no activation; prefix every CLI command with uv run
```

For the full dev stack (Postgres, engine, worker), see [Installation](../../getting-started/installation.md).

## Health check

Verify the engine is reachable before running anything else:

```bash
warden ping
```

(With no activated venv, use `uv run warden ping`.)

A successful response confirms the engine is up and the URL is correct. Add `--json` for the raw health payload.

## Commands

The table below is the operator map. Most day-to-day work uses `deploy`, `start saga`, `list`, and `review`; recovery commands are for stuck steps after automatic timeouts have had time to run.

| Command | What it does |
|---------|--------------|
| `warden deploy` | Register a manifest (worker or saga) with the engine |
| `warden list definitions` | Inspect deployed worker and saga definitions |
| `warden start saga` | Start a saga instance |
| `warden list sagas` | List saga instances and their status |
| `warden list steps` | List step rows for one saga (`--trace-id` required) — status index only |
| `warden show step` | Show one step's inputs, output, prompt ref, and errors (`--step-id` or `step_span_id`) |
| `warden review` | Approve, reject, or HITL-retry steps in `AWAITING_HUMAN` |
| `warden saga retry-step` | Re-queue a stuck forward step (`IN_PROGRESS` on a `RUNNING` saga) |
| `warden saga retry-compensation` | Re-run a failed or stalled compensation step |
| `warden ping` | Health-check the engine |
| `warden --version` | Print the CLI version |

Most `list` commands and `ping` support `--json` for machine-readable output. Use `warden list steps --trace-id … --json` for timing and `error_details` on failed steps. Use `warden show step` for resolved inputs and `output_payload`.

## Operator retry matrix

During an incident, pick the row that matches the step and saga status — do not mix HITL retry with forward or compensation recovery. The same matrix appears in [API overview → Operator retry matrix](../api/overview.md#operator-retry-matrix).

| Action | HTTP | CLI | Step status | Saga status | When to use |
|--------|------|-----|-------------|-------------|-------------|
| HITL re-run | `POST .../retry` | `warden review retry` | `AWAITING_HUMAN` | `AWAITING_HUMAN` | Operator wants the agent to try again with guidance |
| HITL approve/reject | `POST .../decision` | `warden review approve` / `reject` | `AWAITING_HUMAN` | `AWAITING_HUMAN` | Human decision to continue or reject |
| Forward recovery | `POST .../retry-step` | `warden saga retry-step` | `IN_PROGRESS` (stuck) | `RUNNING` | Worker/claim/outbox stall after automatic reap window; use `--force` if a non-stale claim still blocks redelivery |
| Compensation recovery | `POST .../retry-compensation` | `warden saga retry-compensation` | failed/stuck comp | `COMPENSATING` | Undo step failed or stalled |

**Not in this matrix:** LLM transient backoff (`WARDEN_LLM_RETRY_*` in [Configuration](../../getting-started/configuration.md)) and saga restart (`warden start saga`). A bare `retry-step` returns **`claim_active`** when a worker still holds a non-stale claim — wait for automatic reap or pass `--force` (`--allow-destructive` on commit steps). Full recovery ladder: [Saga recovery](saga-recovery.md).

:::info[Enterprise]
When `WARDEN_PLUGINS` is set, `warden audit` is registered automatically. See [Open Core vs Enterprise](../../getting-started/open-core-vs-enterprise.md) for details.
:::

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Failure — unreachable engine, local validation error, or HTTP conflict. For automated pipelines, parse stderr or use `--json` on supported commands. |

## What's next

You now have the command map and retry matrix. [Deploy and list](deploy-and-list.md) registers manifests and confirms what the engine knows about; from there you start instances, watch them run, handle HITL holds, and recover stuck steps in the pages that follow.
