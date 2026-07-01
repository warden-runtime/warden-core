# Warden

**The Postgres-native runtime that keeps AI agents honest.**

> **Early release (`v0.1.0`).** APIs, manifests, and CLI flags may change between releases—pin a tag or commit if you depend on stability. **PyPI** packages and **pre-built container images** (GHCR) are planned; install from this repo via clone, [`uv`](https://docs.astral.sh/uv/), or Docker Compose ([Quick start](#quick-start)).

Warden is an open-core runtime for building resilient, durable, inspectable, and human-governed agent workflows—running entirely inside your own PostgreSQL database.

Building autonomous agents for real work means living with non-determinism. When loops run entirely in ephemeral application memory, they lack a true transaction boundary. If an LLM returns malformed JSON, a third-party API drops mid-sequence, or a worker container crashes, execution state vanishes—you are left with partial side effects and no safe way to resume or recover.

Warden persists a durable **saga state machine** in Postgres. Work becomes explicit, persistent steps—not an opaque in-memory script firing network calls. Bring your agent logic; Warden owns durability, policy, and rollback.

- **Durable state** — Every step is a Postgres row before the workflow advances. If a process dies, you pick up from recorded state, not from scratch.
- **Pre-flight safety** — Enforce policy rules (CEL) on tool arguments and step output inside the engine, before payloads hit your APIs or MCP tools.
- **Human-in-the-loop review** — Pause high-risk mutations for operator approval or rejection; state stays in Postgres until a human decides.
- **Transactional rollbacks** — When a multi-step run fails partway through, orchestrate a **last-in, first-out (LIFO)** compensation loop to undo prior external side effects in order.

---

## Documentation

The source code is only half the story. Architecture, configuration reference, and guided demos live in the documentation.

**[Published docs →](https://warden-runtime.org)** · **[In-repo manual →](docs/introduction.md)** (for local preview and contributors)

| Start here | |
|------------|---|
| [Core concepts](docs/concepts/terminology.md) | Sagas, instances, steps, and durable execution boundaries |
| [Architecture](docs/advanced/architecture.md) | Engine, worker, outbox loop, plugin registry |
| [Manifest guides](docs/guides/manifests/overview.md) | Worker/saga YAML, policies, MCP tools |

Preview the site locally:

```bash
make docs-api   # first time / after engine API changes
cd website && npm install && npm start   # → http://localhost:3000
```

---

## Quick start

**First getting-started demo:** run a complete mock saga locally with no API keys. Continue to [Observe execution timing](docs/getting-started/demo-observe-execution-timing.md), [Quickstart](docs/getting-started/demo-quickstart.md) (live model), and [GitHub MCP](docs/getting-started/demo-github-mcp.md) (policies and human review).

From the repo root (copy [`.env.example`](.env.example) → `.env` first):

```bash
git clone https://github.com/warden-runtime/warden-core.git
cd warden-core

make sync-dev && make up

export ENGINE_URL=http://127.0.0.1:8000

warden deploy -f config/worker.mock-mcp.yaml
warden deploy -f config/saga.mock-mcp.yaml
warden start saga -n mock-mcp-saga -v 0.1.0 --input '{"name":"Ada"}'
warden list steps --trace-id <trace_id>
```

Full walkthrough: [Demo: Mock LLM and MCP](docs/getting-started/demo-mock-llm-and-mcp.md) → [Observe execution timing](docs/getting-started/demo-observe-execution-timing.md) → [Quickstart](docs/getting-started/demo-quickstart.md) → [GitHub MCP](docs/getting-started/demo-github-mcp.md).

```bash
make help        # all operator targets
make sync-dev    # install dependencies
make up          # Postgres + migrate + engine + worker (+ Jaeger, Adminer)
```

MCP demos that spawn Docker stdio servers (e.g. GitHub MCP) use the Compose `worker` service with the host Docker socket mounted—the same `make up` path.

---

## Core architecture

Warden is built entirely around a shared-nothing, bidirectional transactional outbox pattern. The engine and workers are completely decoupled—they never communicate directly, eliminating complex network meshes and external message brokers (like RabbitMQ or Kafka). Instead, a single PostgreSQL table (`outbox_events`) serves as the durable state clearinghouse.

The execution loop flows in a continuous, database-backed cycle:

1. **Control** — The CLI drives and monitors the high-level workflow lifecycle by communicating with the engine over HTTP.
2. **Stage** — The engine advances the finite state machine (FSM) and commits both the new state and a logical `worker-commands` row to the Postgres outbox inside the *same atomic database transaction*.
3. **Execute** — Ephemeral workers poll the outbox for unassigned `worker-commands` rows, claim them, and execute the heavy, non-deterministic LLM or MCP reasoning loops.
4. **Advance** — Once the task is done, the worker writes an `engine-events` row (e.g. `STEP_COMPLETED`) back to the outbox. The engine's background consumer polls for these rows, ingests them, and loops back to step 2 to schedule the next phase.

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

Planning capacity and operational limits: [Architecture → Scaling and operational limits](docs/advanced/architecture.md#scaling-and-operational-limits).

| Path | Role |
|------|------|
| `common/`, `engine/`, `workers/`, `cli.py` | Open-core kernel |
| Plugin registry (`WARDEN_PLUGINS`) | NoOp defaults; install enterprise plugins from the private **warden-enterprise** repo |
| `docs/`, `website/` | Documentation sources and Docusaurus site |

---

## Develop & test

```bash
make check       # ruff, xenon, typecheck, kernel import boundary
make tests       # pytest with coverage (Docker for Postgres slice)
make doctor      # when engine/worker look stuck
```

Contributors: [CONTRIBUTING.md](CONTRIBUTING.md) · [Testing guide](docs/advanced/testing.md)

---

## Community

| | |
|--|--|
| **General** | [authors@warden-runtime.org](mailto:authors@warden-runtime.org) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, PR checklist, open-core boundary |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting — [security@warden-runtime.org](mailto:security@warden-runtime.org) |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Community standards — [conduct@warden-runtime.org](mailto:conduct@warden-runtime.org) |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [LICENSE](LICENSE) | Apache 2.0 |

Licensed under **Apache 2.0** — Copyright 2026 The Warden Authors.
