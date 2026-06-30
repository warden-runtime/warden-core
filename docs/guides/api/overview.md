---
sidebar_position: 1
pagination_prev: guides/cli/saga-recovery
pagination_next: guides/api/deploy-and-list
---

# API overview

The Warden **engine** exposes a versioned HTTP API at `ENGINE_URL`. Workers consume commands from Postgres; the API is the control plane for deploying manifests, starting sagas, reviewing HITL steps, and operator recovery.

This section mirrors the CLI guides with curl examples and poll loops. If you use `warden` day to day, start with [CLI overview](../cli/overview.md) — every CLI command maps to one or more HTTP calls. For a full HTTP walkthrough on a governed saga, see [Demo: GitHub MCP](../../getting-started/demo-github-mcp.md).

## Base URL

```bash
export ENGINE_URL=http://127.0.0.1:8000
```

All routes in this manual use the `/v1` prefix (for example `GET $ENGINE_URL/v1/health`).

## Two documentation layers

| Layer | Where | Purpose |
|-------|-------|---------|
| **Workflow guides** | [API guides](overview.md) (this section) | curl examples, poll loops, CLI mapping |
| **API reference** | [API Reference](/docs/api/api-reference) | Auto-generated from OpenAPI — schemas, parameters, status codes |

On a running engine, interactive Swagger is at `{ENGINE_URL}/docs` (not part of the static docs site).

## Health check

Verify the engine process is up:

```bash
curl -sS "$ENGINE_URL/v1/health"
```

Expected: `{"status":"ok"}`.

CLI equivalent: `warden ping`.

## Async execution model

Warden is a **database-backed coordinator**, not a blocking RPC proxy.

- Most mutating routes that **enqueue work** return **`202 Accepted`** when the request is accepted (start saga, HITL decisions, operator recovery).
- **`POST /v1/manifests`** is the exception: deploy validates and registers the definition **synchronously** and returns **`200`** with a success message.
- **Poll** saga and step status with collection endpoints — see [Start and monitor](start-and-monitor.md).
- There are **no OSS webhooks**; integrators poll (or use OpenTelemetry/Jaeger in parallel — [Observability](../observability.md)).

## Operator retry matrix

Use this table during incidents — do not conflate path suffixes or step statuses.

| Action | HTTP | CLI | Step status | Saga status | When to use |
|--------|------|-----|-------------|-------------|-------------|
| HITL re-run | `POST .../retry` | `warden review retry` | `AWAITING_HUMAN` | `AWAITING_HUMAN` | Operator wants the agent to try again with guidance |
| HITL approve/reject | `POST .../decision` | `warden review approve` / `reject` | `AWAITING_HUMAN` | `AWAITING_HUMAN` | Human decision to continue or reject |
| Forward recovery | `POST .../retry-step` | `warden saga retry-step` | `IN_PROGRESS` (stuck) | `RUNNING` | Worker/claim/outbox stall after automatic reap window; JSON body `{"force": true}` if a non-stale claim still blocks redelivery |
| Compensation recovery | `POST .../retry-compensation` | `warden saga retry-compensation` | failed/stuck comp | `COMPENSATING` | Undo step failed or stalled |

`{trace_id}` and `{step_span_id}` are literal path segments — substitute ids from saga start and step list responses (`trace_id` is 32 lowercase hex chars; `step_span_id` is 16). Example forward recovery:

```bash
curl -sS -X POST "$ENGINE_URL/v1/sagas/7f3a9c2e1b4d8f0a6e5c3b2a1d9f8e7c/steps/a1b2c3d4e5f67890/retry-step?namespace=default" \
  -H "Content-Type: application/json" \
  -d '{}'
```

A **`202`** with `"status": "claim_active"` means a worker still holds a non-stale claim — wait for automatic reap or resend with `"force": true` (commit steps also need `"allow_destructive": true`). See [Recovery](recovery.md).

Supply a client **`recovery_token`** in the JSON body when you need safe HTTP retries after timeouts — duplicate requests with the same token and parameters replay the original **202** response without re-enqueueing work.

**Not in this matrix:** LLM transient backoff (`WARDEN_LLM_RETRY_*` in [Configuration](../../getting-started/configuration.md)) and saga restart (`POST /v1/sagas/start` / idempotency — [Start and monitor](start-and-monitor.md)).

Details: [HITL](hitl.md), [Recovery](recovery.md).

:::info[Enterprise]
When `WARDEN_PLUGINS` is set, audit HTTP routes and matching CLI commands are registered automatically. See [Open Core vs Enterprise](../../getting-started/open-core-vs-enterprise.md).
:::

## Route map (OSS)

| Area | Guide |
|------|-------|
| Deploy + list definitions | [Deploy and list](deploy-and-list.md) |
| Start + poll `trace_id` | [Start and monitor](start-and-monitor.md) |
| HITL queue + decisions | [HITL](hitl.md) |
| Operator recovery | [Recovery](recovery.md) |

## What's next

You have the base URL, async model, and retry matrix. [Deploy and list](deploy-and-list.md) registers manifests and lists definitions; the following pages start sagas, poll until terminal status, handle HITL holds, and recover stuck steps.
