---
sidebar_position: 1
pagination_prev: getting-started/open-core-vs-enterprise
pagination_next: guides/manifests/worker-manifests
---

# Manifests and artifacts

Worker and saga **manifests** are how you declare workflows — they deploy to Postgres. **Artifacts** are the on-disk files your manifests reference: Jinja prompts, CEL policies, JSON Schema, and compensation YAML under `*_ROOT` paths.

Now that you've run the basic demos, this guide helps you write your own workflows. We'll cover how manifests deploy, how artifact paths resolve, and the order to build your first real configuration. Examples match the [GitHub MCP demo](../../getting-started/demo-github-mcp.md) when you want a full walkthrough.

## Deploy and identity

`warden deploy -f <file>` sends your manifest to the engine, which validates and saves it. Warden tracks each definition by a unique combo of `namespace`, `name`, and `version`. If you leave out `namespace` in your YAML, the engine defaults to `"default"`. You can deploy new versions without affecting running sagas; each run tracks `trace_id`, not manifest names.

See [Component identity](../../concepts/terminology.md#component-identity) for how identity fields line up across deploy, start, steps, and runtime.

Prompts, policies, schemas, and compensation files must be visible to **both** engine and worker at the same `*_ROOT` paths — see [Artifact paths](#artifact-paths) below and [Configuration → Disk artifact roots](../../getting-started/configuration.md#disk-artifact-roots) for host vs container mounts.

## Deploy order matters

Deploy workers before sagas. At deploy time, Warden checks every `worker` reference in your saga against what's already in Postgres. If a worker name is missing or mistyped, deploy fails right there. This catch saves you from hitting a runtime crash later because of a simple typo in a worker name.

Workers declare what your stack can do (LLM, MCP). Sagas declare how steps use those capabilities:

```bash
warden deploy -f config/worker.minimal.yaml
warden deploy -f config/saga.minimal.yaml
```

## What's in each manifest type

| Manifest | Stored in | Defines |
|----------|-----------|---------|
| `kind: worker` | `worker_definitions` | LLM provider, model, system prompt, MCP tool sources |
| `kind: saga` | `saga_definitions` | Steps, step kinds, `agent-adapter` on reason steps, tool allowlists, policy refs, HITL gates |

Everything else — Jinja prompts, CEL policies, JSON Schema, compensation YAML — is referenced by path from saga or worker fields and resolved from disk. Details and examples are in [Artifact paths](#artifact-paths).

## Artifact paths

Saga and worker manifests point at on-disk files by **path relative to a `*_ROOT` directory**. Subdirectories are allowed; do not use `..` or absolute paths.

| Step field | Root env var | Manifest example | Resolved path |
|------------|--------------|------------------|---------------|
| `prompt` | `PROMPTS_ROOT` | `triage.j2` | `{root}/triage.j2` |
| `policy` | `POLICIES_ROOT` | `github-issue-comment.yaml` | `{root}/github-issue-comment.yaml` |
| `output_schema` | `SCHEMAS_ROOT` | `triage.json` | `{root}/triage.json` |
| `compensation` | `COMPENSATIONS_ROOT` | `disburse_undo.yaml` | `{root}/disburse_undo.yaml` |

Always use paths **with file extensions** as shown in the table. Subdirectories are allowed (`teams/marketing/gate.yaml`). How engine and worker resolve `*_ROOT` on the host vs in Compose: [Configuration → Disk artifact roots](../../getting-started/configuration.md#disk-artifact-roots).

When you deploy a saga, the engine checks that referenced prompt, policy, schema, and compensation files exist on disk. When a step runs, the worker loads prompts and executes against those paths.

## Authoring pipeline

Here's the order we recommend:

1. **[Worker manifests](worker-manifests.md)** — Set up your LLM provider, MCP tool sources, and base system prompts.
2. **[Saga manifests](saga-manifests.md)** — Add reason and commit steps, tool allowlists, policy refs, and HITL gates.
3. **[Prompts](prompts.md)** — Write Jinja templates for reason steps.
4. **[MCP and tools](mcp-and-tools.md)** — Configure transports, tool allowlists, and resource reads.
5. **[Conditional branching (`when.cel`)](when-cel.md)** — Skip steps based on prior output or tool facts.
6. **[Policies](policies.md)** — CEL guardrails at `after_reason` and `before_commit`.
7. **[Compensation](compensation.md)** — Undo paths when a run fails.

After authoring, use **[Observability](../observability.md)** to inspect runs in Postgres and Jaeger, then the **[CLI](../cli/overview.md)** to operate sagas day to day.
