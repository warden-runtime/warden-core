---
sidebar_position: 3
pagination_prev: guides/manifests/worker-manifests
pagination_next: guides/manifests/saga-manifests
---

# Step manifests

A step manifest is a reusable capability in the catalog: worker pin, tools, prompt, policy, and declared **input ports**. Run `warden deploy -f config/<step-manifest>.yaml` to save it in Postgres under `step_definitions`.

Deploying a step does not run anything. Sagas **compose** catalog steps with `use:` + `version`, bind ports with `with`, and optionally gate with `when`. At saga deploy the engine link-checks those refs; at saga start it hydrates them into instance `frozen_steps` — see [Saga manifests → Composing catalog steps](saga-manifests.md#composing-catalog-steps).

YAML examples below follow the [GitHub MCP demo](../../getting-started/demo-github-mcp.md) shapes (`config/step.github-triage.yaml`, `config/step.github-post-comment.yaml`).

## Required fields

Every step manifest needs identity, `step_kind`, a worker pin, and (for reason) a prompt:

```yaml
kind: step
name: github-triage
namespace: default
version: "0.1.0"
title: Triage open issues
description: Triage open issues (reason + GitHub MCP).
inputs:
  owner:
    required: true
  repo:
    required: true
  focus_issue_number:
    required: false
step_kind: reason
worker: github-demo-worker
worker_version: "0.1.0"
prompt: github-triage.j2
tools:
  allow:
    - name: get_me
    - name: list_issues
    - name: issue_read
output_schema: github-triage-output.json
```

`name`, `namespace`, and `version` identify the saved step definition (see [Manifests and artifacts → Deploy and identity](overview.md#deploy-and-identity)). Saga composition pins it with `use` + `version` in the same namespace.

| Field | Role |
|-------|------|
| `step_kind` | `reason` or `commit` — maps to runtime step `kind` when the saga hydrates this catalog entry |
| `inputs` | Named ports the saga must satisfy via `with` (required ports must be bound) |
| `title` | Operator-facing label; defaults to `name` when omitted |
| `worker` / `worker_version` | Worker definition pin (deploy workers first) |

## Input ports

`inputs` declares the contract for saga `with` bindings. Each key is a port name; `required: true` (default) means the saga ref must supply that key. Optional `description` documents the port for operators.

Optional `schema` is a Draft-7 JSON Schema fragment for the port value:

- **Step deploy** — fragment must be valid and must not use unsupported composition keywords (same rules as `output_schema`)
- **Saga deploy** — `value:` literals are validated against the port schema (`from:` paths are checked at schedule time)
- **Schedule** — resolved `with` values are validated before the worker command is built

Unknown `with` keys and missing required ports fail at **saga** deploy when the ref is link-checked.

Reason and commit are **discriminated** schemas (`step_kind`): reason-only fields (`prompt`, `agent-adapter`, `facts`, …) cannot appear on a commit step, and commit requires `tools` with exactly one allowlisted tool.

## Reason vs commit

| `step_kind` | Requires | Capability highlights |
|-------------|----------|------------------------|
| `reason` | Non-empty `prompt` | `agent-adapter`, `tools.allow`, `facts`, `skills`, token budgets |
| `commit` | Exactly one tool in `tools.allow` | No `prompt`, `facts`, or `agent-adapter` |

Commit example (`config/step.github-post-comment.yaml`):

```yaml
kind: step
name: github-post-comment
namespace: default
version: "0.1.0"
title: Post triage comment
inputs:
  owner:
    required: true
  repo:
    required: true
  issue_number:
    required: true
    schema:
      type: integer
      minimum: 1
  body:
    required: true
    schema:
      type: string
      minLength: 1
step_kind: commit
worker: github-demo-worker
worker_version: "0.1.0"
policy: github-issue-comment.yaml
hitl: true
tools:
  allow:
    - name: add_issue_comment
```

Capability fields (`prompt`, `tools`, `policy`, `hitl`, `facts`, budgets, and so on) live on the **step** manifest. Saga refs only compose: `id`, `use`, `version`, `with`, `when`, and tighten-only overrides. Runtime behavior of reason/commit after start hydrate is documented in [Saga manifests](saga-manifests.md).

## Optional capability fields

Same shapes as the hydrated saga step once an instance starts. Common ones:

| Field | Notes |
|-------|--------|
| `tools` / `resources` / `skills` | Allowlists — see [MCP and tools](mcp-and-tools.md) |
| `output_schema` | JSON Schema path under `SCHEMAS_ROOT` |
| `policy` / `hitl` | Guardrails — see [Policies](policies.md) |
| `facts` | Reason-only tool extractors |
| `compensation` | Undo YAML under `COMPENSATIONS_ROOT` |
| `timeout_seconds` / `max_turns` / token caps | Budgets (tighten-only at saga compose) |

For `agent-adapter: react \| simple`, tool binding, and failure codes, see [Saga manifests → Reason step execution](saga-manifests.md#reason-step-execution-agent-adapter).

## Deploy and list

Deploy workers, then steps, then sagas:

```bash
warden deploy -f config/worker.github-demo.yaml
warden deploy -f config/step.github-triage.yaml
warden deploy -f config/step.github-post-comment.yaml
warden deploy -f config/saga.github-demo.yaml
```

List registered steps:

```bash
warden list definitions --type step
```

HTTP: `GET /v1/definitions/steps` — see [CLI](../cli/deploy-and-list.md) and [API](../api/deploy-and-list.md).

## What's next

[Saga manifests](saga-manifests.md) — compose these steps with `use:`, bind ports, and gate with `when`.
