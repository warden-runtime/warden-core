---
sidebar_position: 2
pagination_prev: guides/cli/overview
pagination_next: guides/cli/start-and-monitor
---

# Deploy and list

Before you can start a saga, the engine must have your worker, step, and saga definitions registered. `warden deploy` validates each manifest and stores the result in Postgres — runtime credentials and MCP connectivity are checked later when steps actually run.

This page covers `warden deploy` and `warden list definitions`. Deploy order is **workers → steps → sagas**: step manifests pin `(worker, worker_version)`; saga `use:` refs pin catalog steps. The engine rejects a deploy if a referenced row is missing.

## Deploy a manifest

```bash
warden deploy -f <path-to-manifest.yaml>
```

Typical order on a fresh stack (GitHub demo shape):

```bash
warden deploy -f config/worker.github-demo.yaml
warden deploy -f config/step.github-triage.yaml
warden deploy -f config/step.github-post-comment.yaml
warden deploy -f config/saga.github-demo.yaml
```

On success, the CLI prints a confirmation message. **Sagas** may redeploy the same `(namespace, name, version)` to update the stored authoring AST (upsert). **Steps and workers** are append-only for that identity — bump `version` to change capability. Saga deploy **link-checks** `use:` refs against the catalog; it does not persist an expanded reason/commit body (hydration runs at saga start into instance `frozen_steps`).

**What deploy checks:**

- YAML structure and required fields
- Worker references on step manifests; step `use:` + `version` on sagas
- Prompt, policy, `output_schema`, and compensation file paths (policy CEL is compile-checked)
- CEL expression syntax in `when` conditions
- `with` keys against step `inputs` (required ports, unknown keys)
- Tighten-only overrides (cannot widen catalog budgets / HITL)

It does **not** validate API keys or MCP server reachability — those surface at step execution time. A policy removed from disk after deploy still fails at gate time (`errored`).

## List definitions

Inspect what is registered:

```bash
warden list definitions --type saga
warden list definitions --type step
warden list definitions --type worker
```

| Flag | Description | Default |
|------|-------------|---------|
| `--type` / `-t` | Required: `saga`, `step`, or `worker` | — |
| `--namespace` | Filter by namespace | — |
| `--name` | Filter by definition name | — |
| `--is-active` | Filter by active status (all definition kinds) | — |
| `--limit` | Max results to return | 50 (max 100) |
| `--offset` | Pagination offset | 0 |

Add `--json` for machine-readable output.

:::note[Definitions vs runtime steps]
`warden list definitions --type step` lists **catalog** step manifests. `warden list steps --trace-id …` lists **runtime** step rows for one saga instance.
:::

Soft-disable a catalog pin without deleting it:

```bash
warden definitions set-active -t worker --id <uuid> --inactive
warden definitions set-active -t step --namespace default --name github-triage --version 0.1.0 --active
```

## What's next

With definitions registered, start a saga instance and watch it progress: [Start and monitor](start-and-monitor.md). The HTTP equivalent is [Deploy and list](../api/deploy-and-list.md) followed by [Start and monitor](../api/start-and-monitor.md).
