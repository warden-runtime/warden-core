---
sidebar_position: 2
pagination_prev: getting-started/prerequisites
pagination_next: getting-started/demo-mock-llm-and-mcp
---

# Installation

This page gets you from a fresh clone to a working local stack. You will start Postgres, the engine, and a worker in Docker, then confirm everything with `warden ping` from your machine. The CLI runs on your host; the services run in Compose.

If you have not read [Prerequisites](prerequisites.md) yet, skim it first — it explains how the CLI, engine, worker, and Postgres fit together. This page stays focused on bringing the stack up.

## Requirements

Warden splits work across your machine and Docker. On the **host**, you install the CLI tooling; Compose brings up everything that talks to Postgres.

- **Python 3.11 or later** — runs `make sync-dev` and the `warden` CLI
- **[uv](https://github.com/astral-sh/uv)** — installs CLI and dev dependencies into `.venv/`
- **Docker** — runs the Compose stack (`make up`)

You do not install Postgres, the engine, or the worker yourself. `docker-compose.yml` builds and starts those services, including a one-shot migration before the engine and worker come online.

## Quick start

Clone the repository, then bring up the stack:

```bash
git clone https://github.com/warden-runtime/warden-core.git
cd warden-core
cp .env.example .env
make sync-dev
make up
# First boot: wait for migrations to finish (often 15–30s). If ping fails, retry or: docker compose logs -f engine
source .venv/bin/activate
export ENGINE_URL=http://127.0.0.1:8000   # or set ENGINE_URL in .env
warden ping
```

Set `ENGINE_URL` in `.env` if you prefer not to export each session. Postgres defaults in `.env.example` are enough to boot; change credentials and API keys when you need them — see [Configuration](configuration.md).

A successful `warden ping` means the engine is listening and the CLI can reach it.

## Local development stack

`make up` starts the getting-started stack from `docker-compose.yml`: Postgres, a one-shot migrate job, engine, worker, Adminer, and Jaeger. Migrations run before engine and worker start — you do not apply SQL by hand on a normal first boot.

:::warning[Development only]
This Compose file is for local development and evaluation — not a production recipe. It includes an unauthenticated engine API, secrets in plain `.env`, Postgres on a host port, and a Docker socket mount for MCP demos. Harden any fork before production use.
:::

The repo also ships `docker-compose.example.yml` as **deployment boilerplate**: Postgres, migrate, engine, and worker only — no Adminer, Jaeger, or dev mounts. Use it as a starting point when you build your own layout; it is not part of this walkthrough.

## Demo roadmap

With the stack up and `warden ping` healthy, work through the four demos below **in order**. Each adds one layer; the timing demo reuses the `trace_id` from the first run.

1. **[Mock LLM and MCP](demo-mock-llm-and-mcp.md)** — Deploy worker and saga manifests, start an instance, and watch the worker execute a multi-turn ReAct step against a local MCP tool. No API keys. Covers the outbox, worker claim path, and core CLI commands.

2. **[Observe execution timing](demo-observe-execution-timing.md)** — Read per-step `execution_timing` buckets on the saga you already started. Reuses the mock demo's `trace_id`.

3. **[Quickstart](demo-quickstart.md)** — Replace the mock LLM with live inference (OpenAI or a local OpenAI-compatible server). Requires an API key or a running local model.

4. **[GitHub MCP](demo-github-mcp.md)** — Policy gates, human-in-the-loop review, conditional steps (`when.cel`), and tool facts against a real GitHub repo. Requires a PAT and LLM credentials. If you cannot run it hands-on, use the read-only walkthrough on that page.

The first three demos skip policy, HITL, and conditional branching so you can see the execution loop first. GitHub MCP adds those governance layers on top.

When you have gone through all four demos, you will have touched deploy, saga lifecycle, worker execution, observability, live inference, and governed workflows — the essentials of how Warden works.

## What's next

Start with [Demo: Mock LLM and MCP](demo-mock-llm-and-mcp.md). If `make up` or `warden ping` fails, see [Troubleshooting](troubleshooting.md).

## Related

- [Configuration](configuration.md) — env catalog, Postgres layout, dev stack ports and Makefile targets
- [Troubleshooting](troubleshooting.md) — `make doctor`, logs, stuck services
