---
sidebar_position: 5
pagination_prev: guides/manifests/prompts
pagination_next: guides/manifests/when-cel
---

# MCP and tools

Workers reach external systems through MCP servers declared on the worker manifest. Saga steps then narrow that surface with per-step allowlists. For a full stdio walkthrough on the dev stack, see [Demo: GitHub MCP](../../getting-started/demo-github-mcp.md); for manifest fields, start with [Worker manifests](worker-manifests.md).

Two layers always apply: **`tool_sources`** on the worker (what MCP endpoints the worker can open) and **`tools.allow`** on each saga step (what a given step may call). Transport choice is the main variable — SSE when something else hosts the server, stdio when the worker should start it. The sections below cover auth headers, tool and resource allowlists, policy interaction, and designing tools for at-least-once delivery.

## Transport: SSE vs stdio

| Transport | When to use | Connection cost |
|-----------|-------------|-----------------|
| **`sse` (default)** | Team already hosts the MCP server (Compose service, k8s sidecar, API gateway) | HTTP connect + MCP `initialize` — no subprocess spawn |
| **`stdio`** | Worker should start the server (local binary, mock fixture, `docker run`) | New process per step connection (~150–200 ms typical) |

Keep in mind that it's your worker process that needs a clear network line to the MCP server, not the core Warden engine. You'll want to make sure your firewall rules, container networks, and DNS settings allow the worker container to talk directly to that server endpoint.

YAML examples: [Worker manifests → MCP tool sources](worker-manifests.md#mcp-tool-sources).

## Hosted MCP authentication (SSE)

When an MCP server sits behind an API gateway or expects bearer-token auth, set `headers` on the SSE `tool_sources` entry. **Do not** put production tokens in manifest YAML — reference worker environment variables:

```yaml
tool_sources:
  - name: company-tools
    transport: sse
    url: https://mcp.internal.example.com/sse
    headers:
      Authorization: "Bearer ${ENV:COMPANY_MCP_TOKEN}"
      X-Api-Key: "${ENV:GATEWAY_KEY}"
```

At step execution the worker resolves `${ENV:VAR}` and `${VAR}` from its process environment. Set those variables on the worker service (`.env`, k8s secrets, Compose `env_file`). Unset variables log a warning and substitute an empty string.

Literal header values (no `${…}` placeholder) work for non-secret metadata. Stdio secrets use a parallel pattern: `env_inherit` / `env` on the subprocess — see [Worker manifests](worker-manifests.md#mcp-tool-sources).

## Tool allowlists

Each saga step has a `tools.allow` list. Each entry may use the **raw MCP tool id** (e.g. `calendar.list_events`) **or** the **provider-safe sanitized form** (e.g. `calendar_list_events`). During execution the worker matches either form against discovered MCP tools.

LLM providers require tool names matching `^[a-zA-Z0-9_-]{1,64}$`. Warden therefore always exposes **sanitized** names on the wire (dots and other illegal characters become `_`; collisions within a step get `_2`, `_3`, …). MCP `call_tool`, governance, policy CEL `tool.name`, and `facts[].tool` keep the **original** MCP ids.

**`react`** reason steps run a multi-turn ReAct loop and can call any tool on their allowlist across multiple turns. The prompt injection `allowed_tools` lists the sanitized wire names (plus `read_resource` / `_submit` when applicable). Use optional `tools.bind` (⊆ `with`) to pin saga-resolved values onto matching MCP args and hide those keys from the model schema — see [Saga manifests → Bindings](saga-manifests.md#bindings-with). **`simple`** reason steps (`agent-adapter: simple`) use no MCP tools — only structured LLM output. Commit steps call exactly one allowed tool and don't invoke an LLM (full `with` → kwargs; no `tools.bind`). The engine requires exactly one `tools.allow` entry when scheduling a commit step. Use commit steps for deterministic, side-effecting actions where you want no agent discretion.

On **`react`** reason steps only, the virtual **`_submit`** tool is always available. It lets the agent signal structured completion without calling an external MCP server. It does not apply to **`simple`** steps. See [Saga manifests → Reason step execution](saga-manifests.md#reason-step-execution-agent-adapter).

Syntax and examples: [Worker manifests](worker-manifests.md) for `tool_sources`, [Saga manifests](saga-manifests.md) for `tools.allow`.

## Skills (`skills.allow`)

Skills are **worker-scoped playbooks** on disk under `SKILLS_ROOT/<worker_name>/<skill_id>.md` (YAML frontmatter + markdown body). They are not MCP tools.

On **`react`** reason steps, optional `skills.allow` lists skill ids the step may load. At execute time the worker:

1. Loads each skill’s frontmatter (`name`, `description`, `allowed_tools`)
2. Unions those `allowed_tools` with the step’s `tools.allow` (**extras**) into the effective MCP allowlist (raw and sanitized MCP ids collapse to one entry; extras win for schemas)
3. Injects a virtual **`load_skill`** tool and puts `allowed_skills: [{name, description}, …]` into the step prompt context (static index)

Do **not** list `load_skill` in `tools.allow` or skill `allowed_tools` — it is reserved like `_submit`. Missing or malformed skill files fail the step with `SKILL_NOT_FOUND` / `SKILL_INVALID` **before** any LLM turn. Incompatible with **`agent-adapter: simple`**. Commit steps and compensation undo YAML do not use skills.

```yaml
  - id: triage
    kind: reason
    worker: github-demo-worker
    prompt: triage.j2
    skills:
      allow:
        - name: triage
    tools:
      allow:
        - name: add_issue_comment   # extra beyond the skill's allowed_tools
```

Example skill file (`config/skills/github-demo-worker/triage.md`):

```markdown
---
name: triage
description: Use when classifying GitHub issues before drafting a comment.
allowed_tools:
  - get_issue
  - list_issues
---
# Triage playbook
...
```

## Resource allowlists (`resources.allow`)

MCP **tools** are callable functions (`list_issues`, `add_issue_comment`, …). MCP **resources** are addressable documents the server exposes by URI — policy files, profile records, static context blobs. Tools and resources are governed separately: `tools.allow` controls which tools the agent may invoke; `resources.allow` controls which resource URIs it may read.

On each saga step, optional `resources.allow` lists URI templates the step may fetch. Each entry needs a `uri`; `description` is optional metadata for authors.

```yaml
  - id: review-risk
    kind: reason
    worker: risk-worker
    prompt: review.j2
    with:
      customer_id:
        from: "$.input.customer_id"
    resources:
      allow:
        - uri: "file:///policies/fraud-v3.md"
        - uri: "postgres://risk/profiles/{customer_id}"
    tools:
      allow:
        - name: score_transaction
```

When `resources.allow` is non-empty on a **`react`** reason step, the worker injects a virtual **`read_resource`** tool (same pattern as `_submit` — you do not list `read_resource` in `tools.allow`). During the ReAct loop the agent calls `read_resource` with a concrete URI; the worker checks the URI against the step allowlist, then fetches content from connected MCP servers. Incompatible with **`agent-adapter: simple`**.

| Concern | Behavior |
|---------|----------|
| **Where declared** | Saga step `resources.allow` (persisted on the step instance row) |
| **MCP dependency** | Worker must have `tool_sources` — resource reads use MCP `read_resource` on those sessions |
| **Parameterized URIs** | `{placeholder}` segments in the template (e.g. `{customer_id}`) must match resolved step arguments from `with` |
| **Traversal / smuggling** | `..`, encoded traversal, and ambiguous overlapping templates are rejected at runtime |
| **Typical use** | Reason steps that need read-only context before or during tool calls — not commit steps (no agent loop) |

Static templates (no placeholders) must match exactly — for example `file:///policies/fraud-v3.md`. Parameterized templates bind placeholders to saga variables: if the agent requests `postgres://risk/profiles/cust-42`, the worker verifies that URI matches `postgres://risk/profiles/{customer_id}` and that `customer_id` in resolved step arguments equals `cust-42`.

Resource reads are read-only boundaries. They do not replace `tools.allow` for side effects — use a **commit** step when the workflow must perform exactly one governed write.

## Execution boundaries

The allowlist is the primary boundary for tool access. [Policies](policies.md) add a second layer — they evaluate step outputs at fixed phases before results are committed or external writes are dispatched.

## Designing for at-least-once delivery

Warden delivers worker commands **at-least-once** — see [Architecture — Idempotency](../../advanced/architecture.md#idempotency) for claim reap and engine dedup. A worker that crashes *after* calling your external API but *before* emitting a result may run the same step again when the claim is reaped.

Assume retry, not exactly-once, when you author MCP tools and commit steps:

- Tools that bill, mutate infrastructure, or send messages must be **idempotent** or keyed by stable platform identifiers (`saga_trace_id`, `step_span_id`, command `idempotency_key`) in your external system.
- Commit steps with side effects and compensation undo handlers share the same boundary — design handlers for safe retry.

## What's next

Next up: [Conditional branching (`when.cel`)](when-cel.md) — learn how to skip steps before they run based on prior context or extracted facts.

## Related

- [Worker manifests → MCP tool sources](worker-manifests.md#mcp-tool-sources) — SSE vs stdio, `${ENV:…}` header placeholders, stdio secrets (`config/worker.minimal.yaml`, `config/worker.github-demo.yaml`)
- [Architecture](../../advanced/architecture.md) — transactional outbox and idempotency mechanisms
- [GitHub MCP demo](../../getting-started/demo-github-mcp.md) — Docker stdio on the dev stack (`/var/run/docker.sock`, compose env)
- [Saga manifests](saga-manifests.md) — `tools.allow` and `with` bindings per step
