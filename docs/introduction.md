---
sidebar_position: 1
sidebar_label: Introduction
pagination_next: concepts/terminology
---

# Warden

**The Postgres-native runtime that keeps AI agents honest.**

Warden is an open-core runtime for building resilient, durable, inspectable, and human-governed agent workflows—running entirely inside your own PostgreSQL database.

Purpose-built for high-risk AI workflows where inspectable execution history, human oversight, and safe failure are requirements—not afterthoughts.

## The problem Warden solves

Building autonomous agents for real work means living with non-determinism. When loops run entirely in ephemeral application memory, they lack a true transaction boundary. If an LLM returns malformed JSON, a third-party API drops mid-sequence, or a worker container crashes, execution state vanishes—you are left with partial side effects and no safe way to resume or recover.

Warden persists a durable **saga state machine** in Postgres. Work becomes explicit, persistent steps—not an opaque in-memory script firing network calls. Bring your agent logic; Warden owns durability, policy, and rollback.

- **Durable state** — Every step is a Postgres row before the workflow advances. If a process dies, you pick up from recorded state, not from scratch.
- **Pre-flight safety** — Enforce [CEL policies](guides/manifests/policies.md) on tool arguments and step output inside the engine, before payloads hit your APIs or [MCP](https://modelcontextprotocol.io/docs/getting-started/intro) tools.
- **Human-in-the-loop (HITL)** — Pause high-risk mutations until an operator approves or rejects; state stays in Postgres until then.
- **Transactional rollbacks** — When a multi-step run fails partway through, orchestrate a **last-in, first-out (LIFO)** compensation loop to undo prior external side effects in order. If a compensation step fails, Warden halts at a deterministic `FAILED` state—see the [Compensation guide](guides/manifests/compensation.md) for failure boundaries and recovery.

**What open core includes:** policy evaluation, human-in-the-loop gates, LIFO compensation, and authoritative saga and step state in Postgres—the kernel, not enterprise plugins.

## Your workflow in three steps

**Define**. Write YAML manifests and deploy them to the engine. Worker manifests declare tools and prompts; saga manifests declare ordered steps and the policy file governing each step.

**Run**. Start a saga instance via the CLI or API. Workers execute each step while the engine records progress in Postgres—you can monitor the run and pick up where it left off after a failure.

**Govern**. Set policy boundaries, approve or reject work at human review gates, and roll back completed steps when a run cannot safely continue. 

## What you're running

Warden runs entirely inside your own infrastructure—no external SaaS, no managed cloud dependency.

The **engine** manages saga state, enforces policies, and stages work on the transactional outbox. **Workers** execute steps—reasoning with an LLM, acting through MCP tools—and report results back through the same outbox. Both are Python services you deploy and operate yourself; they are shared-nothing peers that only meet in Postgres (`worker-commands` and `engine-events` topics on `outbox_events`). Topology, firewall rules, and the four-step execution loop: [Architecture](advanced/architecture.md).

You define workflows in YAML (worker and saga manifests), operate them through the `warden` CLI or HTTP API, and observe everything through Postgres and OpenTelemetry.

:::info[Enterprise]

The open kernel runs standalone. Need a tamper-evident forensic ledger, scheduled governance reapers, or human-in-the-loop SLA enforcement beyond inline recovery? See [Open Core vs Enterprise](getting-started/open-core-vs-enterprise.md).

:::

## Core concepts

| Term | Meaning |
|------|---------|
| **Saga** | A versioned workflow definition (`kind: saga`): steps, tool allowlists, policy and prompt references. Stored under `(namespace, name, version)` |
| **Worker definition** | A versioned manifest (`kind: worker`) describing LLM config and MCP connections. Stored under `(namespace, name, version)` |
| **Instance** | A live saga execution identified by `trace_id`—not by manifest name/version/namespace |
| **Step** | A single unit of work within a saga — a durable Postgres row per step instance (`reason` with `agent-adapter: react \| simple`, or `commit`) |
| **Engine** | Saga FSM, HTTP API, policy and human-in-the-loop gates; writes saga state and `worker-commands` rows; consumes `engine-events` from the outbox |
| **Worker** | Polls and claims `worker-commands` rows, executes reason/commit steps, writes `engine-events` (e.g. `STEP_COMPLETED`) back to the outbox |
| **Policy** | [CEL](https://cel.dev/) rules evaluated at key points in a step—defining the boundaries a step's output must stay within |
| **Tool facts** | Values extracted from MCP tool JSON into saga context (`steps.<id>.facts.<into>`)—see [Tool facts (`facts`)](guides/manifests/saga-manifests.md#tool-facts-facts) |
| **Outbox** | The `outbox_events` table—Postgres is the message bus; engine and worker exchange commands and results through logical topics, not direct RPC |
| **Compensation** | On saga failure, unwinds completed forward steps in reverse order |

Definition vs runtime identity: [Component identity](concepts/terminology.md#component-identity).

## Choose your path

The sidebar is ordered concepts first, then hands-on. Understanding Warden's transaction model upfront means policy gates and `AWAITING_HUMAN` in the live demos will feel familiar when you reach them.

**Recommended.** Work through [Core concepts](concepts/terminology.md) ([Terminology](concepts/terminology.md) → [Durable execution boundaries](concepts/durable-execution.md) → [Lifecycle](concepts/lifecycle.md)), then Setup ([Prerequisites](getting-started/prerequisites.md) and [Installation](getting-started/installation.md)), then all four Demos in order — [Mock LLM and MCP](getting-started/demo-mock-llm-and-mcp.md) → [Observe Execution Timing](getting-started/demo-observe-execution-timing.md) → [Quickstart](getting-started/demo-quickstart.md) → [GitHub MCP](getting-started/demo-github-mcp.md). Keep [Configuration](getting-started/configuration.md) and [Troubleshooting](getting-started/troubleshooting.md) open when something in the environment breaks.

**After Getting started.** [Guides → Manifests and artifacts](guides/manifests/overview.md) cover worker and saga YAML, on-disk artifacts, CLI, and API workflows. [Advanced](advanced/architecture.md) covers architecture and [testing](advanced/testing.md).

## Try it locally

Want hands-on before the full manual? Clone the repo, bring up the stack, and run the bundled **mock** saga—no API keys. First boot usually takes a few minutes (Docker pull, build, migrations); once `warden ping` succeeds, deploy and start take about a minute.

**Prerequisites:** Python 3.11+, uv, and Docker — see [Installation](getting-started/installation.md).

```bash
git clone https://github.com/warden-runtime/warden-core.git
cd warden-core
cp .env.example .env
make sync-dev && make up
source .venv/bin/activate
export ENGINE_URL=http://127.0.0.1:8000
warden ping
```

If `ping` fails right after `make up`, wait for migrations to finish and retry — [Installation → Quick start](getting-started/installation.md#quick-start).

```bash
warden deploy -f config/worker.mock-mcp.yaml
warden deploy -f config/saga.mock-mcp.yaml
warden start saga -n mock-mcp-saga -v 0.1.0 --input '{"name":"Ada"}'
warden list steps --trace-id <trace_id>
```

Copy `trace_id` from the `start` response. Both steps should show `COMPLETED`.

**Next steps**

- Something failed? [Installation](getting-started/installation.md) and [Troubleshooting](getting-started/troubleshooting.md)
- What did you just run? [Demo: Mock LLM and MCP](getting-started/demo-mock-llm-and-mcp.md) — manifests, prompts, and success criteria
- Prefer concepts first? Follow **Recommended** above
