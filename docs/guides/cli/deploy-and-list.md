---
sidebar_position: 2
pagination_prev: guides/cli/overview
pagination_next: guides/cli/start-and-monitor
---

# Deploy and list

Before you can start a saga, the engine must have your worker and saga definitions registered. `warden deploy` validates each manifest and stores the result in Postgres — runtime credentials and MCP connectivity are checked later when steps actually run.

This page covers `warden deploy` and `warden list definitions`. Deploy the worker manifest first: saga steps reference `(worker, worker_version)` and inherit the saga's namespace; the engine rejects a saga deploy if the worker row is missing.

## Deploy a manifest

```bash
warden deploy -f <path-to-manifest.yaml>
```

Typical order on a fresh stack:

```bash
warden deploy -f config/worker.minimal.yaml
warden deploy -f config/saga.minimal.yaml
```

On success, the CLI prints a confirmation message. Redeploying the same `(namespace, name, version)` updates the stored definition — deploy is idempotent.

**What deploy checks:**

- YAML structure and required fields
- Worker references in saga steps
- Prompt, policy, `output_schema`, and compensation file paths (policy CEL is compile-checked)
- CEL expression syntax in `when` conditions

It does **not** validate API keys or MCP server reachability — those surface at step execution time. A policy removed from disk after deploy still fails at gate time (`errored`).

## List definitions

Inspect what is registered:

```bash
warden list definitions --type saga
warden list definitions --type worker
```

| Flag | Description | Default |
|------|-------------|---------|
| `--type` / `-t` | Required: `saga` or `worker` | — |
| `--namespace` | Filter by namespace | — |
| `--name` | Filter by definition name | — |
| `--is-active` | Saga definitions only: filter by active status | — |
| `--limit` | Max results to return | 50 (max 100) |
| `--offset` | Pagination offset | 0 |

Add `--json` for machine-readable output.

## What's next

With definitions registered, start a saga instance and watch it progress: [Start and monitor](start-and-monitor.md). The HTTP equivalent is [Deploy and list](../api/deploy-and-list.md) followed by [Start and monitor](../api/start-and-monitor.md).
