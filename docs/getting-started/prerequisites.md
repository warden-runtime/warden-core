---
sidebar_position: 1
pagination_prev: concepts/lifecycle
pagination_next: getting-started/installation
---

# Prerequisites

Warden is a Postgres-backed saga engine: an **engine** orchestrates workflows, a **worker** executes steps (LLM + MCP), and you drive both from the **`warden` CLI** on your host. This page maps how those pieces connect and where manifests vs on-disk files live. Read it before [Installation](installation.md) if you are new here.

## Architecture

Warden operates as a decoupled system. A functional local environment requires Postgres and three discrete processes—the engine, worker, and CLI. The engine and worker **never communicate directly**; they exchange work through a single `outbox_events` table in Postgres (no RabbitMQ, Kafka, or engine→worker HTTP).

```text
  [ warden CLI ]
        │
      HTTP
        ▼
   [ Engine ] ◄─────── polls: engine-events ───────┐
        │                                          │
 writes: worker-commands                           ▼
        │                                     [ Postgres ]
        ▼                                   (outbox_events)
   [ Worker ] ◄──── polls: worker-commands ────────│
        │                                          │
        └─────────── writes: engine-events ────────┘
```

The CLI talks to the engine over HTTP. Step execution flows engine → outbox → worker → outbox → engine—not CLI → worker, and not engine → worker over the network. Full topology and the four-step execution loop: [Architecture](../advanced/architecture.md).

### File resolution

Workflow definitions deploy into Postgres; prompts, policies, and schemas stay on disk. If your sagas use reason steps, make sure both the engine and worker can see your prompts directory — the engine checks those files when you register a workflow, and the worker loads the templates when it runs a step. Under `make up`, leave `PROMPTS_ROOT` unset in `.env`: Compose sets `/app/prompts` inside containers and mounts `./config/prompts` there.

```text
               ┌────────────────────────────────────────┐
               │         Your host / disk volume        │
               │  (PROMPTS_ROOT, POLICIES_ROOT, etc.)   │
               └───────────┬────────────────┬───────────┘
                           │                │
            reads schemas/ │                │ reads Jinja2
            policies       │                │ prompts at runtime
                           ▼                ▼
                     ┌──────────┐      ┌──────────┐
 [ warden CLI ] ────►│  Engine  │      │  Worker  │
                     └────┬─────┘      └────▲─────┘
                          │                 │
                  deploys │ stores definitions
                  YAML    ▼                 │ claims work via
                     ┌──────────────────────┴─────┐
                     │          Postgres          │
                     │  (saga/worker_definitions) │
                     └────────────────────────────┘
```

<details>
<summary>Why Postgres and disk split?</summary>

When you deploy a manifest, Warden saves that blueprint to Postgres so it can start execution instances later. Prompts, policies, and schemas stay as files under your `*_ROOT` paths — usually tracked in Git. Keeping them as plain files makes it easy to review diffs and run templates through your normal CI/CD pipelines.

See [Component identity](../concepts/terminology.md#component-identity) for how definitions and running instances are keyed.

</details>

Host vs container `*_ROOT` paths, volume mounts, and the full variable table are in [Configuration → Disk artifact roots](configuration.md#disk-artifact-roots). Sample files in this repo live under `./config/`.

## What's next

You know the moving parts and where files live. Continue to [Installation](installation.md) to bring up Postgres, engine, and worker with Docker Compose.

The mock demos run safely without deep conceptual prep. If you skipped [Durable execution boundaries](../concepts/durable-execution.md) and [Lifecycle](../concepts/lifecycle.md), read them before [Demo: GitHub MCP](demo-github-mcp.md)—that is where policy gates, HITL pauses, and compensation become essential to follow what is happening.

## Related

- [Installation](installation.md)
- [Concepts: Terminology](../concepts/terminology.md)
- [Configuration](configuration.md)
