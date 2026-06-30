---
sidebar_position: 1
pagination_prev: introduction
pagination_next: concepts/durable-execution
---

# Terminology

You'll see the same core terms cross paths everywhere—from the YAML manifests you author to the CLI outputs and raw API events you monitor during a live run. The [Introduction](../introduction.md) summarizes them at a glance; this page defines each term and clears up the identity collisions you hit when deploying, starting runs, or reading status rows.

**In a hurry?** The [Introduction](../introduction.md#core-concepts) table is enough to run the [mock demo](../getting-started/demo-mock-llm-and-mcp.md). Come back here when you want to dig further into how Warden models workflows — definitions, live instances, and the names you'll see in deploy, start, and status output.

## Core terms

### Saga

A versioned workflow blueprint: ordered steps, per-step tool allowlists, and references to prompts and policies. You deploy a saga definition once and start many instances from it. See [Saga manifests](../guides/manifests/saga-manifests.md).

### Instance

A single live run of a saga definition. Each instance has its own step progress, status, and [saga context](#saga-context). Instances are identified by `trace_id`, not by manifest name or version. Many instances of the same saga can run at once.

### Step

One unit of work in a running saga. **Reason** steps produce structured LLM output. **Commit** steps call exactly one MCP tool, usually to change something outside Warden. Each step is a durable row for the life of the run. See [Reason step execution](../guides/manifests/saga-manifests.md#reason-step-execution-agent-adapter).

### Worker definition

The manifest that declares LLM provider settings, model, MCP tool sources, and system prompt for a worker process. Saga steps pin a worker by name and version at execution time. This is a deployed blueprint, not a running process. See [Worker manifests](../guides/manifests/worker-manifests.md).

### Worker

The running process that executes steps. The engine dispatches work through the [outbox](#outbox); the worker claims each command, loads the pinned worker definition, and runs the LLM and MCP tools for that step. Add replicas to handle more concurrent steps.

### Saga context

The accumulated JSON on each running saga instance. [Bindings](#bindings-with), [conditional branching](#conditional-branching-whencel), and later steps all read from it.

- **`input`** — The payload passed when the saga was started.
- **`steps.<id>.output.data`** — Structured output from a completed step.
- **`steps.<id>.facts.<into>.<field>`** — Values copied from MCP tool responses on reason steps. See [Tool facts](#tool-facts).

As steps complete, the engine adds each step's output and facts under that step's id. Later steps read what came before; earlier entries are append-only.

**What is not in saga context:** the full multi-turn LLM chat transcript from a `react` reason step. Only the validated **`output.data`** envelope (and optional **`facts`** from MCP tools) is merged into `context.steps`. Enterprise reasoning audit, when enabled, stores transcript hashes separately — see [Open Core vs Enterprise](../getting-started/open-core-vs-enterprise.md). For the schedule-time handoff into commit steps, see [Saga manifests → Reason → commit boundary](../guides/manifests/saga-manifests.md#reason-commit-boundary).

### Bindings (`with`)

Optional keys on a saga step that pull values from [saga context](#saga-context). When the step is scheduled, the engine resolves them into a flat `arguments` map. On **reason** steps, those keys hydrate the prompt template. On **commit** steps, they become MCP tool arguments. See [Bindings](../guides/manifests/saga-manifests.md#bindings-with).

### Conditional branching (`when.cel`)

Branching lets you dynamically skip steps that don't apply to a specific run. By placing a [CEL](https://cel.dev/) expression on a step, the engine evaluates the current [saga context](#saga-context) before routing work to a worker.

- **If the expression evaluates to true:** The step runs normally.
- **If it evaluates to false:** The engine short-circuits the step, marks it `SKIPPED`, and moves to the next step instantly without wasting network or worker cycles.

Use `when.cel` to skip steps based on initial input, prior step outputs, or extracted [tool facts](#tool-facts). If you need to halt and block a step after an agent has already generated arguments or outputs, use [Policies](#policy) instead. See [Conditional branching (`when.cel`)](../guides/manifests/when-cel.md).

### Policy

**Policy** enforces hard rules on what a step may do next — after the worker returns output or commit arguments are resolved, but before the saga advances or an external tool call proceeds. Each rule is a [CEL](https://cel.dev/) expression in a YAML file attached to the step.

[Conditional branching](#conditional-branching-whencel) decides whether a step runs. Policy **judges** a step that is already scheduled. If the rule passes, the step continues. If it fails, Warden marks the step `FAILED` and [compensation](#compensation) may run. See [Policies](../guides/manifests/policies.md).

### Outbox

The reliable queue between the engine and workers. State changes and handoff messages are written together so work is not lost between services. See [Lifecycle](lifecycle.md).

### Compensation

When a saga cannot finish safely, the engine undoes completed forward steps in reverse order using logic you define for each step. See [Compensation](../guides/manifests/compensation.md).

### Tool facts

Selected values copied from MCP tool output into [saga context](#saga-context). Later steps can branch or bind on real tool data, not only on LLM output. See [Tool facts](../guides/manifests/saga-manifests.md#tool-facts-facts).

## Component identity

Warden enforces a strict boundary between your static design-time configurations and your active real-time executions. If you are deploying files or checking the status of an active workflow, you are interacting with two distinct types of identity:

**Definitions (design-time).** Your YAML manifests are static blueprints. They are versioned and tracked by a composite key of `namespace`, `name`, and `version`. If a namespace is omitted from your YAML, Warden defaults it to `"default"`.

```yaml
# config/saga.minimal.yaml
namespace: default
name: minimal-saga
version: 0.0.1
```

**Instances (runtime).** The moment you execute a saga, it leaves the blueprint stage and becomes a live, isolated execution path tracked exclusively by a `trace_id`.

You use definitions to manage code changes, but you use the `trace_id` to audit, pause, or troubleshoot a live run:

```bash
# Target the blueprint definition to start a run:
warden start saga -n minimal-saga -v 0.0.1 --namespace default

# Target the resulting runtime instance to inspect live state:
warden list sagas --trace-id <TRACE_ID> --namespace default
warden list steps --trace-id <TRACE_ID> --namespace default
```

## Status enums

| Enum | Values |
|------|--------|
| `SagaStatus` | `PENDING`, `RUNNING`, `AWAITING_HUMAN`, `COMPENSATING`, `COMPLETED`, `FAILED`, `COMPENSATED` |
| `StepStatus` | `PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `COMPENSATING`, `COMPENSATED`, `SKIPPED`, `TIMED_OUT`, `AWAITING_HUMAN` |

These values appear in API responses, CLI output, and Postgres directly. [Lifecycle](lifecycle.md) maps how instances move through them.

## What's next

With these terms in hand, the next pages explain why they matter in practice. [Durable execution boundaries](durable-execution.md) covers volatile workers vs durable Postgres state and LIFO rollback when a run fails. [Lifecycle](lifecycle.md) walks through saga and step states, the outbox handoff between engine and worker, and the status values you will see in CLI output.

## Related

- [Introduction](../introduction.md)
- [Durable execution boundaries](durable-execution.md)
- [Lifecycle](lifecycle.md)
- [Migrations and schema](../advanced/migrations-and-schema.md)
- [Compensation](../guides/manifests/compensation.md)
