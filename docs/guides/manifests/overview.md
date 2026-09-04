---
sidebar_position: 1
pagination_prev: getting-started/open-core-vs-enterprise
pagination_next: guides/manifests/worker-manifests
---

# Manifests and artifacts

Worker, step, and saga **manifests** are how you declare workflows — they deploy to Postgres. **Artifacts** are the on-disk files your manifests reference: Jinja prompts, CEL policies, JSON Schema, and compensation YAML under `*_ROOT` paths.

Now that you've run the basic demos, this guide helps you write your own workflows. We'll cover how manifests deploy, how artifact paths resolve, and the order to build your first real configuration. Examples match the [GitHub MCP demo](../../getting-started/demo-github-mcp.md) when you want a full walkthrough.

## Deploy and identity

`warden deploy -f <file>` sends your manifest to the engine, which validates and saves it. Warden tracks each definition by a unique combo of `namespace`, `name`, and `version`. If you leave out `namespace` in your YAML, the engine defaults to `"default"`. You can deploy new versions without affecting running sagas; each run tracks `trace_id`, not manifest names.

See [Component identity](../../concepts/terminology.md#component-identity) for how identity fields line up across deploy, start, steps, and runtime.

Prompts, policies, schemas, and compensation files must be visible to **both** engine and worker at the same `*_ROOT` paths — see [Artifact paths](#artifact-paths) below and [Configuration → Disk artifact roots](../../getting-started/configuration.md#disk-artifact-roots) for host vs container mounts.

## Deploy order matters

Deploy **workers → steps → sagas**. At deploy time, Warden checks every `worker` pin on a step definition, and every `use:` + `version` on a saga, against what's already in Postgres. Missing or mistyped names fail at deploy instead of at runtime.

Workers declare LLM and MCP capacity. Step manifests declare reusable capabilities (prompt, tools, policy, input ports). Sagas compose those steps into a workflow; deploy link-checks refs and start freezes hydrated steps on the instance:

```bash
warden deploy -f config/worker.github-demo.yaml
warden deploy -f config/step.github-triage.yaml
warden deploy -f config/step.github-post-comment.yaml
warden deploy -f config/saga.github-demo.yaml
```

## What's in each manifest type

| Manifest | Stored in | Defines |
|----------|-----------|---------|
| `kind: worker` | `worker_definitions` | LLM provider, model, system prompt, MCP tool sources |
| `kind: step` | `step_definitions` | Reusable capability: `step_kind`, inputs, worker pin, tools/prompt/policy/HITL |
| `kind: saga` | `saga_definitions` | Composition via `use:` + `version` + `with` + `when` (authoring AST; hydrate at start) |

Everything else — Jinja prompts, CEL policies, JSON Schema, compensation YAML — is referenced by path from step or worker fields (and validated when those manifests deploy) and resolved from disk. Details and examples are in [Artifact paths](#artifact-paths).

## Artifact paths

Step and worker manifests point at on-disk files by **path relative to a `*_ROOT` directory**. Subdirectories are allowed; do not use `..` or absolute paths.

| Manifest field | Root env var | Manifest example | Resolved path |
|----------------|--------------|------------------|---------------|
| `prompt` | `PROMPTS_ROOT` | `github-triage.j2` | `{root}/github-triage.j2` |
| `policy` | `POLICIES_ROOT` | `github-issue-comment.yaml` | `{root}/github-issue-comment.yaml` |
| `output_schema` | `SCHEMAS_ROOT` | `github-triage-output.json` | `{root}/github-triage-output.json` |
| `compensation` | `COMPENSATIONS_ROOT` | `disburse_undo.yaml` | `{root}/disburse_undo.yaml` |

Always use paths **with file extensions** as shown in the table. Subdirectories are allowed (`teams/marketing/gate.yaml`). How engine and worker resolve `*_ROOT` on the host vs in Compose: [Configuration → Disk artifact roots](../../getting-started/configuration.md#disk-artifact-roots).

When you deploy a **step** (or a saga that link-checks those fields), the engine checks that referenced prompt, policy, schema, and compensation files exist on disk. When a step instance runs, the worker loads prompts and executes against those paths.

## Authoring pipeline

Here's the order we recommend:

1. **[Worker manifests](worker-manifests.md)** — Set up your LLM provider, MCP tool sources, and base system prompts.
2. **[Step manifests](step-manifests.md)** — Declare reusable reason/commit capabilities and input ports.
3. **[Saga manifests](saga-manifests.md)** — Compose catalog steps with `use:`, `with`, and `when`.
4. **[Prompts](prompts.md)** — Write Jinja templates for reason steps.
5. **[MCP and tools](mcp-and-tools.md)** — Configure transports, tool allowlists, and resource reads.
6. **[Conditional branching (`when.cel`)](when-cel.md)** — Skip steps based on prior output or tool facts.
7. **[Loop blocks (`until`)](loops.md)** — Bounded do-while over nested catalog step refs.
8. **[Child sagas (`spawn_sagas` / `join_sagas`)](child-sagas.md)** — Fan out to child saga instances and wait-all join.
9. **[Policies](policies.md)** — CEL guardrails at `after_reason` and `before_commit`.
10. **[Compensation](compensation.md)** — Undo paths when a run fails.

After authoring, use **[Observability](../observability.md)** to inspect runs in Postgres and Jaeger, then the **[CLI](../cli/overview.md)** to operate sagas day to day.
