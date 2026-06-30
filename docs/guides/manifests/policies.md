---
sidebar_position: 7
pagination_prev: guides/manifests/when-cel
pagination_next: guides/manifests/compensation
---

# Policies

Policies are CEL rules the engine evaluates at fixed points in a step's lifecycle. They live as YAML files under `POLICIES_ROOT` and are referenced by **path** from saga steps (`policy: my-gate.yaml`), not by `(namespace, name, version)` like saga definitions.

Only the engine evaluates policies — never the worker. Unlike an LLM, CEL rules are entirely deterministic—the model can't hallucinate its way around them, interpret them creatively, or override your guardrails. The engine evaluates the exact same expression against the same inputs and gives you an identical pass/fail result every single time. The only thing that changes is *when* it checks: reasoning steps are evaluated right after the worker finishes (`after_reason`), while commit steps are evaluated right before a tool call leaves the engine (`before_commit`). On commit steps, `before_commit` is the hard gate — bad arguments never reach an external system. On **`react`** reason steps, MCP tools on the allowlist may already have run before `after_reason` fires; for a single irreversible side effect with a hard gate, use a **commit** step.

The sections below cover policy files, attachment syntax, CEL bindings, outcomes, and how policies differ from [`when.cel`](when-cel.md).

End-to-end example: [GitHub MCP demo](../../getting-started/demo-github-mcp.md).

## Policy files

Each policy is a YAML file under `POLICIES_ROOT` (default `./config/policies` in the repo). Paths like `./config/policies` are relative to your **repository root** (where the engine runs)—not to this documentation file's location in the docs tree. The file must contain a non-empty `cel:` expression. Optional metadata:

| Field | Purpose |
|-------|---------|
| `name` | Display name (defaults to the manifest ref minus extension, preserving subdirs) |
| `version` | Optional label on the policy artifact (read at gate time; the step row stores the `policy:` path ref, not this field) |
| `cel` | CEL expression — must evaluate to **bool** (`true` = pass, `false` = deny) |

```yaml
# config/policies/github-issue-comment.yaml
name: github-issue-comment
version: "1"
cel: "phase == 'before_commit' && step.kind == 'commit' && step.id == 'post-comment' && tool.name == 'add_issue_comment' && arguments.owner == input.owner && arguments.repo == input.repo && arguments.issue_number > 0 && size(arguments.body) > 0 && size(arguments.body) <= 8000 && arguments.body.contains('## Warden triage')"
```

The engine loads the file when the gate runs. `warden deploy` also validates that each referenced policy file exists and that its CEL compiles. A policy that goes missing after deploy surfaces as `errored` at runtime. Keep policy files on disk where the **engine** container can read them — see [Configuration](../../getting-started/configuration.md) for `POLICIES_ROOT` and Compose mounts.

CEL expressions evaluate to `true` or `false` — in a policy gate, `true` passes and `false` denies. For syntax, operators, and functions, see the [CEL documentation](https://cel.dev).

## Attaching to a saga step

Reference a policy by path on any step that should be gated:

```yaml
steps:
  - id: post-comment
    kind: commit
    worker: github-demo-worker
    policy: github-issue-comment.yaml
    # with, tools, when, hitl, … omitted — see saga manifests
```

Field placement and `with` bindings are saga-manifest concerns — see [Saga manifests](saga-manifests.md).

## Evaluation phases

The engine chooses the evaluation phase from the step's `kind` — you do not set `after_reason` or `before_commit` in the saga manifest or policy file.

| Phase | When | Step type | What CEL can inspect |
|-------|------|-----------|----------------------|
| `after_reason` | After the worker returns structured reason-step output, before it is merged into saga context | Reason | `output` (validated business object), `arguments` (resolved step inputs) |
| `before_commit` | Before `DO_COMMIT` is queued to the worker | Commit | `arguments` (resolved `with` → MCP tool args), `tool.name` |

On **`react`** reason steps, MCP tools on the allowlist may already have run during the ReAct loop before `after_reason` fires. For a single irreversible side effect with a hard gate, use a **commit** step and `before_commit`.

## CEL evaluation context

At gate time the engine exposes these top-level names in policy `cel:`:

| Name | Contents |
|------|----------|
| `phase` | `"after_reason"` or `"before_commit"` |
| `input` | Saga start payload (`context.input`) |
| `arguments` | Resolved step inputs — on commit steps, the MCP argument map from `with` |
| `output` | Reason-step structured output in `output.data` (empty on `before_commit`) |
| `saga` | `trace_id`, `namespace`, `status` |
| `step` | `id`, `name`, `kind`, `order_index` |
| `worker` | `name` (worker manifest name) |
| `tool` | `name` of the single allowed tool (commit steps only) |

Policy CEL does **not** receive `steps.*` from saga context. To gate on a prior step's output or facts, bind the values you need into `with` (commit) or read them from the current step's `output` / `arguments` (reason). The GitHub demo policy checks `arguments.owner`, `arguments.body`, etc., because `with` already resolved them from earlier steps.

### Policy vs `when.cel`

Both use CEL, but they are different gates with different bindings. Full `when.cel` reference: [Conditional branching](when-cel.md).

| | `when.cel` on a step | `policy:` on a step |
|--|----------------------|---------------------|
| What does it ask? | Should this step run at all? | May this step proceed past the gate? |
| Evaluated when? | Before the step is scheduled | `after_reason` or `before_commit` |
| If false? | Step `SKIPPED`; saga continues | Step `FAILED`; compensation if needed |
| `steps` in binding? | Yes — full `context.steps` | No |
| Typical use? | Branching, optional steps | Validate agent output or block bad commit args |

Policy files are validated (load + CEL compile) at saga registration as well.

## Outcomes

CEL evaluates to `true` or `false`. `true` → **`passed`**; `false` → **`denied`**. If the expression cannot run (missing file, parse error, wrong type), the outcome is **`errored`** — treated like a denial for the step.

| Outcome | When | Effect |
|---------|------|--------|
| `passed` | CEL is `true` | Step continues |
| `denied` | CEL is `false` | Step fails (`POLICY_REASON_DENIED` or `POLICY_COMMIT_DENIED`); compensation if needed |
| `errored` | CEL did not evaluate to bool | Step fails (`POLICY_EVALUATION_FAILED`); same halt as denial |

## Denial vs HITL

Denial and human-in-the-loop review are independent. A policy denial fails the step — it doesn't automatically pause for human review. HITL is configured separately with `hitl: true` on the step. See [HITL review](../cli/hitl-review.md) for how that flow works.

You can use both on the same step: the policy gate runs first, and HITL only triggers if the policy passes.

## What's next

Next up: [Compensation](compensation.md) — declare automatic rollback strategies and safety nets for when things go wrong mid-workflow.

## Related

- [Conditional branching (`when.cel`)](when-cel.md) — schedule-time CEL gates
- [Saga manifests](saga-manifests.md) — `with` bindings, reason vs commit
- [MCP and tools](mcp-and-tools.md) — allowlists and commit-step boundaries
- [GitHub MCP demo](../../getting-started/demo-github-mcp.md) — `before_commit` policy on commit arguments
