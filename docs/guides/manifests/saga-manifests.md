---
sidebar_position: 3
pagination_prev: guides/manifests/worker-manifests
pagination_next: guides/manifests/prompts
---

# Saga manifests

A saga manifest is your workflow blueprint. Warden tracks each one by `namespace`, `name`, and `version` — see [Component identity](../../concepts/terminology.md#component-identity). It declares steps, how data flows between them, tool allowlists, and policy/HITL gates. YAML examples below follow the [GitHub MCP demo](../../getting-started/demo-github-mcp.md) shape unless noted otherwise. In this repo, the saga file is `config/<saga-manifest>.yaml`; prompts, policies, schemas, and compensation files it references live under `config/` as `config/<file>`.

Even though individual steps can be skipped, paused, or failed by things like human reviews and conditional logic, the core structure of your workflow stays stable. The manifest always dictates the exact forward order of execution, which worker handles which step, and the strict rule that commit steps handle exactly one tool call. The sections below walk through the fields you'll use to configure that structure, starting with step types.

## Step kinds

Every step is either `reason` or `commit`. Each step needs a unique `id` and a `name`:

| Field | Role |
|-------|------|
| `id` | Stable identifier in saga context, CEL, and `with` bindings (`steps.triage`, `$.steps.triage.output…`) |
| `name` | Human-readable label for operators and logs |

A `reason` step sends work to an LLM-backed worker to produce structured JSON output. By default, this uses **`agent-adapter: react`**, which lets the agent loop through multiple tool calls until it achieves its goal and invokes the built-in `_submit` tool. If you don't need a tool loop and just want a single, direct response from the model, set **`agent-adapter: simple`** instead. See [Reason step execution (agent-adapter)](#reason-step-execution-agent-adapter). Requires `worker`, `worker_version`, and `prompt`.

A `commit` step makes one deterministic MCP call with no LLM loop. Use it for side effects — posting a comment, triggering a webhook, writing a record. It requires `worker` and `worker_version` only (no `prompt`).

```yaml
steps:
  - id: triage
    name: Triage open issues
    kind: reason
    worker: github-demo-worker
    worker_version: "1.0.0"
    prompt: triage.j2

  - id: post-comment
    name: Post triage comment
    kind: commit
    worker: github-demo-worker
    worker_version: "1.0.0"
```

## Reason step execution (agent-adapter)

Reason steps choose **how** the worker completes the step — separate from the worker manifest's `adapter` field (usually `langchain`).

| YAML field | Where | Meaning |
|------------|-------|---------|
| `adapter: langchain` | [Worker manifest](worker-manifests.md#optional-fields) | Which agent runtime implementation the worker loads |
| `agent-adapter: react \| simple` | Reason step in saga manifest | Execution strategy **inside** that port |

### Decision matrix

| Use case | Step kind | `agent-adapter` | `tools.allow` | How output is produced |
|----------|-----------|-----------------|---------------|------------------------|
| Tool-heavy agents ([GitHub demo](../../getting-started/demo-github-mcp.md)) | `reason` | `react` (default) | MCP tool ids | ReAct loop → virtual `_submit` |
| Connectivity / single-turn transforms ([Quickstart](../../getting-started/demo-quickstart.md)) | `reason` | `simple` | must be `[]` | Single structured LLM turn → JSON |
| Deterministic side effect | `commit` | — | exactly one tool | MCP only, no LLM |

### `react` (default)

Multi-turn ReAct loop. The worker binds MCP tools from `tools.allow` plus a virtual **`_submit`** tool (never list `_submit` in the allowlist). The model calls tools until it invokes `_submit` with a non-empty JSON object. Optional `output_schema` validates that payload; without it, any non-empty JSON shape is accepted.

Warden admits sloppy LLM JSON against MCP tool `inputSchema` values and against `_submit` `output_schema` (stringified arrays/objects, scalar strings to numbers/booleans) before strict validation. See [Configuration → LLM JSON admission](../../getting-started/configuration.md#llm-json-admission).

### `simple`

Single structured LLM completion — no ReAct loop, no virtual `_submit`, no MCP tools. Warden rejects the deploy if you set `agent-adapter: simple` with non-empty `tools.allow`, `resources.allow`, or `facts:`.

When `output_schema` is omitted, the worker applies a built-in fallback schema requiring a **`summary`** string (`steps.<id>.output.data.summary`). Set `output_schema` when downstream bindings need stable field names. Structured payloads also go through [LLM JSON admission](../../getting-started/configuration.md#llm-json-admission) before validation.

```yaml
# config/saga.minimal.yaml — live inference smoke test
steps:
  - id: step1
    kind: reason
    agent-adapter: simple
    worker: minimal-worker
    worker_version: "1.0.0"
    prompt: noop.j2
    tools:
      allow: []
```

Contrast with the GitHub triage step (default `react`, tools + `_submit`):

```yaml
  - id: triage
    kind: reason
    # agent-adapter: react   # default — omit in YAML
    worker: github-demo-worker
    worker_version: "1.0.0"
    prompt: triage.j2
    output_schema: github-triage-output.json
    tools:
      allow:
        - name: list_issues
        - name: issue_read
```

### Failure codes by strategy

| `agent-adapter` | Typical `error_details` | Meaning |
|-----------------|---------------------------|---------|
| `react` | `no_submit_call` | Model finished with text only — no `_submit` |
| `react` | `empty_submit_result` | `_submit` called with `{}` |
| `simple` | `structured_output_failed` | Model response was not parseable JSON (common on weak local models) |
| `simple` | `empty_structured_result` | Parsed JSON object was empty |
| either | `STEP_TOKEN_LIMIT_EXCEEDED` | Accumulated provider `total_tokens` exceeded `max_step_tokens` (or `WARDEN_MAX_STEP_TOKENS`) |
| either | `validation: output_schema` / `OUTPUT_SCHEMA_VALIDATION_FAILED` | Payload failed JSON Schema |

Optional context fields on `error_details` (CLI `warden list steps --errors` / `warden show step`):

| Field | When present |
|-------|----------------|
| `reason`, `turns_used`, `last_assistant_content` | `no_submit_call` when the model exited with prose instead of `_submit` (`reason: model_text_exit`) |
| `reason`, `turns_used`, `last_tool_errors` | `no_submit_call` when tool output matched MCP failure heuristics (e.g. `MCP error: …`) |
| `tokens_used`, `max_step_tokens`, `prompt_tokens`, `completion_tokens` | `STEP_TOKEN_LIMIT_EXCEEDED` |
| `tool_result_preview` | `FACT_EXTRACTION_FAILED` / `TOOL_RESULT_TRUNCATED` when tool text explains the failure |
| `truncation_limit` | `TOOL_RESULT_TRUNCATED` when JSON was cut at the worker record limit |
| `response_preview` | `structured_output_failed` on `simple` steps |
| `message` | All normalized failures — human-readable summary |

### Persistence and redeploy

When you start a saga, Warden saves the chosen `agent_adapter` on each step row. Deploy a new manifest version and start a fresh run to pick up a different strategy — **running sagas** keep what they started with.

## Connecting workers to steps

Each step points at a worker with `worker` (name) and `worker_version`. Steps don't declare their own `namespace` — Warden uses the parent saga's namespace to look up `(namespace, worker, worker_version)`. Cross-namespace references fail when you deploy. Deploy workers before sagas: [Manifests and artifacts → Deploy order matters](overview.md#deploy-order-matters).

Later sections add `tools.allow`, `resources.allow`, `with`, and other fields to these same steps. Worker manifests define LLM and MCP config — see [Worker manifests](worker-manifests.md).

## Tool allowlists

`tools.allow` lists the MCP tools a step may call. The worker rejects any tool not on the list.

| Step kind | Allowlist rule |
|-----------|----------------|
| `reason` | Zero or more tools — list every tool the agent is allowed to use |
| `commit` | Exactly one tool — the side effect to execute |

The worker manifest declares which MCP servers exist (`tool_sources` on the worker definition). The saga step declares what **this step** may use. When the step runs, the worker connects to those sources, discovers tool ids from the MCP server, and loads only the names in `tools.allow`. Execution strategy (`react` vs `simple`) is set per reason step — see [Reason step execution (agent-adapter)](#reason-step-execution-agent-adapter).

On a **commit** step, there is no agent loop. The worker calls the one allowed tool directly, using arguments from the step's `with` bindings.

Names must match MCP tool ids exactly. If a listed tool is not exposed by the connected server, the step fails at runtime — Warden does not probe MCP connectivity when you deploy. See [Worker manifests](worker-manifests.md) for `tool_sources` and [MCP and tools](mcp-and-tools.md) for the full execution model.

```yaml
steps:
  - id: triage
    name: Triage open issues
    kind: reason
    worker: github-demo-worker
    worker_version: "1.0.0"
    prompt: triage.j2
    tools:
      allow:
        - name: list_issues
        - name: issue_read

  - id: post-comment
    name: Post triage comment
    kind: commit
    worker: github-demo-worker
    worker_version: "1.0.0"
    tools:
      allow:
        - name: add_issue_comment
```

## Resource allowlists (`resources.allow`)

Optional on **`react`** reason steps when the agent needs read-only MCP context (policy text, profile records) before or during tool calls. Incompatible with **`agent-adapter: simple`**. Commit steps have no ReAct loop, so `resources.allow` is rarely useful there.

Each entry under `resources.allow` requires a `uri` (and optional `description`). URI templates may include `{placeholders}` — the worker binds each placeholder to a **resolved `with` value** when the agent calls the virtual `read_resource` tool. Do not list `read_resource` in `tools.allow`; the worker injects it when this block is non-empty.

```yaml
  - id: review-risk
    kind: reason
    worker: risk-worker
    worker_version: "1.0.0"
    prompt: review.j2
    with:
      customer_id:
        from: $.input.customer_id
    resources:
      allow:
        - uri: "file:///policies/fraud-v3.md"
        - uri: "postgres://risk/profiles/{customer_id}"
    tools:
      allow:
        - name: score_transaction
```

The worker must have MCP `tool_sources` — resource reads go through connected MCP servers, not arbitrary filesystem paths on the engine host. For traversal rules, parameterized URI matching, and how `read_resource` fits the ReAct loop, see [MCP and tools → Resource allowlists](mcp-and-tools.md#resource-allowlists-resourcesallow).

## Bindings (`with`)

Add a `with` block when a step needs data from the saga's start input or from steps that already finished. Each key becomes a named value for the step — a prompt variable on `reason` steps, an MCP tool argument on `commit` steps.

Warden resolves your `with` blocks right before it kicks off the step. It uses standard JSONPath syntax (`$.…`) to grab data from the saga's initial input or any outputs saved by earlier steps, and passes that combined context into the new step. Use `from` for JSONPath lookups, or `value` for a literal.

```yaml
steps:
  - id: triage
    name: Triage open issues
    kind: reason
    worker: github-demo-worker
    worker_version: "1.0.0"
    prompt: github-triage.j2
    with:
      repo:
        from: $.input.repo
    tools:
      allow:
        - name: list_issues
        - name: issue_read

  - id: post-comment
    name: Post triage comment
    kind: commit
    worker: github-demo-worker
    worker_version: "1.0.0"
    with:
      body:
        from: $.steps.triage.output.data.comment_body
    tools:
      allow:
        - name: add_issue_comment
```

### What you can fetch

Warden evaluates bindings right before the step runs, against the saga's `context` object. JSONPath expressions must start with `$`.

| Source | JSONPath | What you get |
|--------|----------|--------------|
| Saga start input | `$.input.<field>` | A field from the JSON passed to `warden start saga --input` |
| Prior step result | `$.steps.<step_id>.output.data.<field>` | Structured JSON from a completed step — reason steps (`react` or `simple`), MCP JSON on commit steps |
| Prior step tool facts | `$.steps.<step_id>.facts.<into>.<field>` | A value extracted from an MCP tool result on an earlier **reason** step — only when that step declared `facts:` and the tool ran. See [Tool facts](#tool-facts-facts) for how `tool`, `into`, and `fields` are declared. |
| Literal | `value: <any>` | A fixed value — no JSONPath lookup |

## Reason → commit boundary \{#reason-commit-boundary\}

When a **reason** step finishes, the worker sends a `STEP_COMPLETED` envelope back to Warden — typically `{ "data": { … }, "facts": { … }? }`. Warden validates `output_schema` against the inner **`data`** object, runs `after_reason` policy if you configured one, then saves:

| Stored where | Contents |
|--------------|----------|
| `saga_step_instances.output_payload` | Normalized envelope (`data` + optional `facts`) |
| `saga.context.steps.<step_id>` | Same shape: `output.data` and `facts` for JSONPath / `with` bindings |

The **ReAct message history** (system, human, assistant, tool turns) is **not** copied into saga context. Downstream steps — including **commit** — only see what you bind explicitly from `output`, `facts`, or `input`.

When a **commit** step is about to run, Warden resolves its `with` block against the current saga context **before** dispatching the MCP call. That flat map becomes:

- `saga_step_instances.resolved_arguments` on the commit step row
- `arguments` on the worker command (single MCP tool invoke — no LLM loop)

```yaml
  - id: post-comment
    kind: commit
    with:
      body:
        from: $.steps.triage.output.data.comment_body   # structured reason output only
    tools:
      allow:
        - name: add_issue_comment
```

Custom **`AgentAdapterPort`** implementations must emit the same envelope on success; Warden never forwards raw adapter-internal state across steps. If you need a field at commit time, expose it in `output.data` or `facts`, then bind it in `with`.

Examples using `triage` → `post-comment`:

```yaml
with:
  owner:
    from: $.input.owner
  summary:
    from: $.steps.triage.output.data.summary
  open_count:
    from: $.steps.triage.facts.triage_metrics.total_count
  priority:
    value: high
```

Only bind from steps that have **already finished** in forward order. When a saga starts, Warden pre-initializes every step id with empty `output.data` and `facts`, so a path to a future or incomplete step resolves to `{}` or `null`. Tool fact paths stay absent until the extractor's tool actually ran — use `when.cel` with `has(...)` for optional branches instead of relying on bindings alone. To populate `facts` buckets on a reason step, see [Tool facts](#tool-facts-facts).

Once resolved, `with` values feed the step at run time. On **commit** steps they become MCP tool arguments. On **reason** steps they hydrate the Jinja prompt — see [Prompts](prompts.md) for how bindings become template variables and what Warden checks when you deploy.

:::info[Context scoping]
Saga context is **append-only per step id**. When a step completes, Warden merges its output under `steps.<step_id>` — it does not mutate other steps' buckets. A later step cannot change what an earlier step stored.
:::

## Policies

Add `policy: <path>` on a step — a path relative to `POLICIES_ROOT` with extension (e.g. `github-issue-comment.yaml` or `teams/marketing/gate.yaml`). Warden loads and validates the file when you deploy, then evaluates it at the gate. See [Policies](policies.md) for file format, CEL binding, phases, and outcomes.

```yaml
  - id: post-comment
    kind: commit
    worker: github-demo-worker
    worker_version: "1.0.0"
    policy: github-issue-comment.yaml
    tools:
      allow:
        - name: add_issue_comment
```

Walkthrough with `before_commit` CEL and HITL: [GitHub MCP demo](../../getting-started/demo-github-mcp.md).

If a policy denies a step or evaluation errors, Warden marks the step `FAILED` and stops forward progress. Depending on how far the saga got, it may flip to `COMPENSATING` and run your declared undo steps backward (LIFO). See [Compensation](compensation.md).

## Human-in-the-Loop (HITL)

Add `hitl: true` to pause a step for operator review. Warden sets the saga to `AWAITING_HUMAN` until someone approves, rejects, or retries. If the step also has a `policy`, the policy gate runs first — HITL only applies when the policy passes. See [Policies](policies.md#denial-vs-hitl).

The hold point depends on step kind:

| Step kind | When the pause happens | What the reviewer sees |
|-----------|------------------------|------------------------|
| `reason` | After the worker returns structured output | The validated reason-step payload in `output.data` (editable on approve) |
| `commit` | Before the MCP tool is called | The resolved `with` arguments — the side effect has not run yet |

On `post-comment`:

```yaml
  - id: post-comment
    name: Post triage comment
    kind: commit
    worker: github-demo-worker
    worker_version: "1.0.0"
    hitl: true
    with:
      body:
        from: $.steps.triage.output.data.comment_body
    tools:
      allow:
        - name: add_issue_comment
```

Optional retry limits while the step is held:

```yaml
    hitl_max_retries: 2
    hitl_retry_guidance: "Tighten the comment and cite the issue number."
```

`hitl_max_retries` caps how many times an operator may call `warden review retry` (omit for unlimited). `hitl_retry_guidance` is default text merged into the worker run as `_hitl_retry.guidance`; per-request `--guidance` on the CLI overrides it.

**Operator actions** (via `warden review` or the human-gate HTTP API):

| Action | Effect |
|--------|--------|
| Approve | Saga resumes — context merges on reason steps; commit tool dispatches on commit steps |
| Reject | Step fails; Warden runs compensation on completed forward steps (LIFO) |
| Retry | Re-runs the worker/LLM while still `AWAITING_HUMAN` (reason steps only in practice; respects `hitl_max_retries`) |

There is no built-in reviewer UI — the kernel exposes CLI and HTTP only. For commands, API paths, and async outbox behavior, see [HITL review](../cli/hitl-review.md). End-to-end example: [GitHub MCP demo](../../getting-started/demo-github-mcp.md).

## Step budgets

`reason` steps accept two independent caps:

| Field | Applies to | Default | Meaning |
|-------|------------|---------|---------|
| `max_turns` | **`react` only** | **10** (max **200**) | Cap on back-and-forth tool/LLM rounds. **`simple`** ignores it (always one LLM call). |
| `max_step_tokens` | **`react` and `simple`** | unlimited (omit / null) | Financial guardrail: abort when accumulated provider-reported **`total_tokens`** (prompt + completion across the step) exceed this budget. |

`max_step_tokens` counts **gross physical tokens** from the provider usage metadata — not cache-discounted billed tokens. Prompt caching can make the invoice much smaller than the counted total; the budget still uses the raw counter. Compensation loops **never** enforce this budget (hydrate always passes unlimited) so rollbacks are not cut short mid-cleanup.

Optional process-wide fallback: set worker env `WARDEN_MAX_STEP_TOKENS` (see [Configuration](../../getting-started/configuration.md)). It applies only when the step omits `max_step_tokens`. Unset or `0` means no fallback.

When the budget is exceeded, the step fails with `error_details.code: STEP_TOKEN_LIMIT_EXCEEDED` (includes `tokens_used`, `max_step_tokens`, `prompt_tokens`, `completion_tokens`). Usage from completed LLM turns is still written to `execution_usage` on `STEP_FAILED`.

`timeout_seconds` is a safety clock for step execution (default **600** seconds). If a worker claims a step and then crashes or hangs, Warden waits for this window to expire, marks the step `FAILED`, and can trigger compensation — it won't auto-retry a stuck step. See [Saga recovery](../cli/saga-recovery.md) for how the open kernel vs enterprise handle timeouts and stale claims.

On `triage`:

```yaml
  - id: triage
    kind: reason
    worker: github-demo-worker
    worker_version: "1.0.0"
    prompt: triage.j2
    max_turns: 15
    max_step_tokens: 50000
    timeout_seconds: 600
    tools:
      allow:
        - name: list_issues
        - name: issue_read
```

Compensation steps inherit `max_turns` from the forward step unless you override it in the compensation file. Compensation YAML lives at `config/<compensation-file>.yaml` (via `COMPENSATIONS_ROOT`; see [Compensation](compensation.md#declaring-compensation)). Token budgets do not apply to compensation.

## Structured output (`output_schema`)

Reason steps can require a fixed JSON shape for worker output in `output.data`. Set `output_schema` to the schema **filename** (relative to `SCHEMAS_ROOT`) — a [JSON Schema](https://json-schema.org/) `.json` file at `config/<schema-file>.json`.

| `agent-adapter` | What gets validated |
|-----------------|---------------------|
| `react` | `_submit` payload after the ReAct loop |
| `simple` | Structured completion from the single LLM turn |

On `triage` (`react`):

```yaml
  - id: triage
    kind: reason
    worker: github-demo-worker
    worker_version: "1.0.0"
    prompt: triage.j2
    output_schema: github-triage-output.json
    tools:
      allow:
        - name: list_issues
        - name: issue_read
```

When you start a saga, Warden resolves `config/<schema-file>.json` and stores the schema on the step row. The worker validates output **before** sending `STEP_COMPLETED`. Warden validates again when it ingests that completion event.

Without `output_schema`: **`react`** still requires a non-empty `_submit` payload (any JSON shape); **`simple`** uses the built-in fallback requiring `summary`.

:::tip[When you need a schema]
Omitting `output_schema` is fine for single-step smoke tests on **`simple`**. For anything that chains steps, treat a schema as the contract between the agent and saga context.

Downstream steps read prior results through paths like `$.steps.triage.output.data.comment_body` in `with` bindings, `when.cel`, and policy gates. HITL on a reason step exposes the same structured `output.data` object for review. If field names or types drift between runs, those bindings resolve to `{}` or `null`, or fail when the step schedules. A JSON Schema file fixes the shape Warden validates before the output lands in context.

Use [tool facts](#tool-facts-facts) when you need structured data from MCP tool JSON instead of from reason-step output (`react` only).
:::

| Outcome | `react` | `simple` |
|---------|---------|----------|
| Valid structured JSON | Proceeds (policy, optional HITL, context merge) | Same |
| Missing / empty output | `no_submit_call` / `empty_submit_result` | `structured_output_failed` / `empty_structured_result` |
| Schema mismatch | `STEP_FAILED` with validation error in `error_details` | Same |

`max_turns` bounds ReAct iterations on **`react`** only. It does **not** grant extra attempts when schema validation fails — an invalid payload fails the step on that run.

Commit steps can also attach `output_schema` for tool result validation.

## Conditional steps (`when`)

Optional `when.cel` on a forward step runs **before** Warden schedules the step. `false` skips the step; a runtime evaluation error fails it with `WHEN_EVALUATION_FAILED`. Syntax, CEL bindings, examples, and troubleshooting: [Conditional branching (`when.cel`)](when-cel.md).

```yaml
    when:
      cel: "has(steps.triage.facts.triage_metrics) && steps.triage.facts.triage_metrics.total_count > 0"
```

Policy gates use a **different** CEL binding (no `steps` root) — see [Policies](policies.md).

## Tool facts (`facts`)

On **`react`** reason steps only (`agent-adapter: simple` rejects `facts:` when you deploy), `facts` copies selected values out of **MCP tool JSON** into saga context after the ReAct loop finishes. Extraction uses whatever the agent actually called during the step — not the structured reason-step output in `output.data`.

Each extractor has three parts:

| Key | What it is | What it does |
|-----|------------|--------------|
| `tool` | MCP tool id | Which tool call to read from. Must match a name in `tools.allow` **and** a call the agent made during this step. If the agent never called that tool, this extractor is skipped entirely. |
| `into` | Bucket name you choose | Groups the extracted fields under `steps.<step_id>.facts.<into>`. Use a short, stable id (e.g. `triage_metrics`) — this is your saga-context name, not the MCP tool name. |
| `fields` | Map of saga key → JSONPath | For each entry, the **left** key is the name you use in `when.cel` and `with` (`total_count`). The **right** value is JSONPath into the tool's JSON response (`$.totalCount`). |

```text
MCP tool JSON (list_issues)     Manifest facts block              Saga context fragment
────────────────────────────    ────────────────────────          ───────────────────────────────────
{                               - tool: list_issues               steps.triage.facts.triage_metrics
  "totalCount": 3,                into: triage_metrics                .total_count = 3
  "issues": [...]                 fields:
}                                   total_count: "$.totalCount"
```

Walkthrough for `triage`:

```yaml
    facts:
      - tool: list_issues
        into: triage_metrics
        fields:
          total_count: "$.totalCount"
```

1. During `triage`, the agent calls the `list_issues` MCP tool (allowed in `tools.allow`).
2. The tool returns JSON — for example `{"totalCount": 3, "issues": [...]}`.
3. The worker runs `$.totalCount` on that JSON and stores the result as `total_count`.
4. Saga context gets `steps.triage.facts.triage_metrics.total_count == 3`.
5. A later step can gate on it: `when.cel: "has(steps.triage.facts.triage_metrics) && steps.triage.facts.triage_metrics.total_count > 0"`.

**MCP tool response** (`list_issues` return value):

```json
{
  "totalCount": 3,
  "issues": [
    {"number": 1, "title": "Example issue"}
  ]
}
```

**Saga context fragment** after extraction (simplified — `output.data` holds the reason-step `_submit` payload separately):

```json
{
  "steps": {
    "triage": {
      "output": { "data": { "summary": "..." } },
      "facts": {
        "triage_metrics": {
          "total_count": 3
        }
      }
    }
  }
}
```

Use `steps.triage.facts.triage_metrics.total_count` in `when.cel` and `with.from` — not `totalCount` (raw tool JSON) or `list_issues` (tool id).

On `triage` (full step context):

```yaml
  - id: triage
    kind: reason
    worker: github-demo-worker
    worker_version: "1.0.0"
    prompt: triage.j2
    tools:
      allow:
        - name: list_issues
        - name: issue_read
    facts:
      - tool: list_issues
        into: triage_metrics
        fields:
          total_count: "$.totalCount"
```

:::warning[Three different names]
`tool: list_issues` must match the **MCP tool id**. `into: triage_metrics` is your **facts bucket** in saga context — they need not match. In `fields`, the **JSONPath** reads the provider's JSON (`$.totalCount`); the **field key** is what you write in saga context (`total_count`). Use `steps.triage.facts.triage_metrics.total_count` in `when` and `with`, not `totalCount` or `list_issues`.
:::

| Behavior | Result |
|----------|--------|
| Tool never called | `into` bucket omitted — no entry under `steps.<id>.facts.<into>` |
| Tool called, JSONPath matches | Values stored at `steps.<id>.facts.<into>.<field>` |

If the tool runs but your JSONPath doesn't match anything in the response, Warden fails the step with `FACT_EXTRACTION_FAILED`.

End-to-end example: [GitHub MCP demo](../../getting-started/demo-github-mcp.md).

## What's next

Every reasoning step needs a prompt template file to guide the agent. Head over to [Prompts](prompts.md) to write Jinja templates and map your `with` blocks into template variables.

## Related

- [MCP and tools](mcp-and-tools.md) — tool vs resource allowlists, `read_resource`, transport
- [Worker manifests](worker-manifests.md)
- [Prompts](prompts.md)
- [Compensation](compensation.md)
- [Policies](policies.md)
