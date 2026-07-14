---
sidebar_position: 4
sidebar_label: Extending Warden
pagination_prev: advanced/architecture
pagination_next: advanced/migrations-and-schema
---

# Extending Warden

Most teams extend Warden without forking the kernel. If you want to plug in a new LLM provider or experiment with a custom agentic reasoning loop, you work directly with **worker ports**. If you need to fire webhooks on saga updates, inject custom API endpoints, or observe policy outcomes in a sidecar, you register **registry hooks** beside the engine/worker loop.

:::note[Prerequisite]
Read [Architecture](architecture.md) first for the plugin registry, boot sequence, and outbox model. This page is the hands-on guide for implementing against those slots.
:::

## Two ways to extend

| Approach | When to use | You implement |
|----------|-------------|---------------|
| **Worker ports** | New LLM provider or agent execution loop | `ChatModelPort` or `AgentAdapterPort` under `workers/` |
| **Registry hooks** | Observe or augment lifecycle without replacing the worker loop | Hook protocols in `common/plugins/` + `register_*` from a `WARDEN_PLUGINS` install function |

As a rule of thumb, stick to **worker ports** for core LLM and execution logic. Step into **registry hooks** when you are introducing new public API endpoints, custom CLI tooling, global event listeners, or a non-Postgres messaging backend. Slot names and boot order: [Architecture → Plugin architecture](architecture.md#plugin-architecture).

## Worker ports

Warden uses the word **adapter** in three places — they are not interchangeable:

| Layer | Interface | Selected by | Shipped today |
|-------|-----------|-------------|---------------|
| **LLM adapter** | `ChatModelPort` | Worker manifest `provider` → `build_llm()` | `openai`, `anthropic`, `local`, `mock` |
| **Agent adapter** | `AgentAdapterPort` | Worker manifest `adapter` (optional, default `langchain`) → `resolve_adapter()` | `langchain` only |
| **Reason strategy** | Inside `AgentAdapterPort` | Saga step `agent-adapter: react \| simple` (default `react`) | `react`, `simple` on `langchain` |
| **Adapter hooks** | `AdapterHooks` (registry `adapter` slot) | `register_adapter_hooks()` at plugin install | NoOp default |

Add an **LLM adapter** when you integrate a new model API. Add an **agent adapter** when you replace the worker's reason/commit execution port (MCP binding, structured output, compensation ReAct). **`agent-adapter` on saga steps** chooses `react` vs `simple` inside the shipped `langchain` port — not a separate worker manifest field. Use **adapter hooks** only to observe or extend behavior after a reason step without replacing the loop.

### Add an LLM provider

`ChatModelPort` in `common/llm/protocol.py` is the contract your provider must satisfy:

```python
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Self

from common.llm import ChatMessage, ChatResponse, ToolProtocol


class ChatModelPort(ABC):
    @abstractmethod
    def bind_tools(self, tools: Sequence[ToolProtocol]) -> Self: ...

    @abstractmethod
    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse: ...
```

**Steps:**

1. Implement `ChatModelPort` under `workers/llm/` (reference: `workers/llm/openai.py`, `workers/llm/anthropic.py`, `workers/llm/mock.py`).
2. Register the provider name in `workers/llm/factory.py` → `build_llm()`.
3. Set `provider: <name>` on the worker manifest and document any new env vars in [Configuration](../getting-started/configuration.md).

An unsupported `provider` fails at worker startup with `ValueError` from `build_llm()`.

### Add an agent adapter

`AgentAdapterPort` in `common/agent_adapter.py` owns reason, commit, and compensation execution. The shipped `LangChainAdapter` branches on saga-step `agent_adapter`: **`react`** runs the multi-turn ReAct loop with MCP tools and virtual `_submit`; **`simple`** runs a single tiered structured LLM call with no tools.

At minimum, a custom adapter implements three async entry points:

```python
from common.agent_adapter import AgentAdapterPort, CompensationResult, StepResult


class MyAgentAdapter(AgentAdapterPort):
    async def run_step(self, *, system_prompt: str, prompt_template: str, arguments: dict, ...) -> StepResult:
        # ReAct or structured reason-step loop — honor tool allowlists and output_schema
        ...

    async def run_commit(self, *, arguments: dict, tool_specs: list[dict], ...) -> StepResult:
        # Single governed MCP tool call — no LLM
        ...

    async def run_compensation(self, *, system_prompt: str, prompt_template: str, arguments: dict, ...) -> CompensationResult:
        # Undo step execution
        ...
```

See `common/agent_adapter.py` for the full parameter list and adapter contract (allowlists, output envelope shape, `ExecutionStepError` on tool failure).

Today every shipped worker manifest omits `adapter` — deploy stores `langchain` from the schema default, and `resolve_adapter()` uses that. You only add `adapter: <name>` to YAML after you register a second implementation in `workers/adapter_resolver.py`.

**Steps:**

1. Implement `AgentAdapterPort` under `workers/adapters/` (reference: `workers/adapters/langchain.py`).
2. Register the adapter name in `workers/adapter_resolver.py` → `resolve_adapter()`.
3. Set `adapter: <name>` on the worker manifest.

An unknown `adapter` fails at worker startup with `ValueError` from `resolve_adapter()`.

### How the worker combines both

When a command is claimed, the flow is always the same:

1. Worker loads `WorkerDefinition` and provider secret.
2. `resolve_adapter()` returns an `AgentAdapterPort` (today: `LangChainAdapter`).
3. The agent adapter calls `build_llm(provider=..., model_name=..., api_key=...)` for a `ChatModelPort`.
4. `run_step` / `run_commit` / `run_compensation` execute through the agent adapter. Optional `AdapterHooks.after_reason_step` runs when a plugin registers hooks.

Manifest fields: [Worker manifests](../guides/manifests/worker-manifests.md).

## Registry hooks

When worker ports are not enough, implement hook protocols under `common/plugins/` and register them from an install function loaded at boot.

### Activating extensions with `WARDEN_PLUGINS`

Warden discovers your hooks through the **`WARDEN_PLUGINS`** environment variable. The value is a single Python entry point in **`module.path:callable`** form — the loader imports the module and calls the function with no arguments:

```bash
# OSS example — no plugins (default)
unset WARDEN_PLUGINS

# Enterprise ledger, audit routes, and CLI extensions
export WARDEN_PLUGINS=enterprise.bootstrap:install

# Your package — point at an install() that calls register_* helpers
export WARDEN_PLUGINS=my_package.bootstrap:install
```

Only **one** entry point is supported per process today. If the value is missing `:`, startup fails with `ValueError: WARDEN_PLUGINS must be module.path:callable`. If import or your install function raises, the process exits before consumers start.

Minimal install skeleton:

```python
# my_package/bootstrap.py
from common.plugins.registry import register_engine_hooks


def install() -> None:
    register_engine_hooks(MyEngineHooks())
    # register_http_extensions(...), register_cli_extensions(...), etc.
```

:::info[Boot lifecycle]
Engine and worker entrypoints call `load_plugins_from_env()` **before** wiring messaging and starting outbox consumers. Plugin install runs once per process. If your `install()` blocks or raises, the process halts immediately — the kernel never starts polling with a half-registered registry.
:::

Entrypoints invoke hooks at named call sites — for example `get_registry().engine.on_saga_transition(...)` after the FSM commits. Full slot list and NoOp defaults: [Architecture → Plugin architecture](architecture.md#plugin-architecture).

Common `register_*` helpers:

| Helper | Slot |
|--------|------|
| `register_engine_hooks` | Saga/step lifecycle observers |
| `register_policy_hooks` | Policy gate outcomes |
| `register_worker_hooks` | Worker command loop |
| `register_adapter_hooks` | After reason-step execution |
| `register_tool_hooks` | MCP tool governance |
| `register_http_extensions` | Extra FastAPI routes |
| `register_cli_extensions` | Extra Typer command groups |
| `register_messaging_factory` | Custom message bus behind the same saga loop |

Reference implementation: private **warden-enterprise** repository (`enterprise.bootstrap:install`).

### Custom messaging backends

Warden's default transport is the **Postgres transactional outbox** — saga state and the next command or event are written in one database transaction, and engine/worker processes poll `outbox_events` on two topics. That is the supported open-core production path: documented, tested, and what the getting-started demos run on. For most deployments, Postgres is the right choice.

#### When you might use something else

A separate message bus enters the picture when **coordination volume** outgrows a single Postgres primary — many worker replicas across zones, higher fan-out, or an existing Kafka or SQS estate you want Warden to plug into. The database remains the source of truth for saga and step state; the bus carries **delivery** of commands and results after commit, usually through an outbox relay. Warden's messaging model was shaped for that kind of distributed relay; the open kernel ships Postgres as the built-in, zero-broker path.

**Enterprise-maintained plugins** are the intended route for broker-backed relay at fleet scale (Kafka, SQS, consumer groups, and related operational tooling). See [Open Core vs Enterprise → Fleet scale and alternate messaging](../getting-started/open-core-vs-enterprise.md#fleet-scale-and-alternate-messaging).

#### Building your own

You can register a custom `MessagingFactory` via `WARDEN_PLUGINS` if you are integrating a bus in-house. The registry hook swaps how messages are produced and consumed; it does not change saga semantics. Whatever transport you use must preserve the same delivery contract the default stack relies on — at-least-once dispatch, idempotent handling on the worker and engine sides, and clear terminal status when a message is done. Those guarantees are what make duplicate delivery safe and stuck work recoverable. Background reap and operator commands still apply when something hangs; see [Architecture → Idempotency](architecture.md#idempotency) and [Saga recovery](../guides/cli/saga-recovery.md).

## Tests and quality gates

Map tests to the kind of extension you ship — that keeps the kernel safe as custom code lands beside it.

| Change | Start here |
|--------|------------|
| LLM provider | `tests/unit/test_llm_factory.py` — verify `build_llm()` resolves your provider |
| Agent adapter | `tests/unit/test_agent_adapter.py` — verify `resolve_adapter()` and port behavior |
| Registry hooks | `tests/unit/test_plugins_registry.py` (messaging: `tests/unit/test_messaging_factory.py`) |
| Engine + worker outbox wiring | `tests/integration/test_worker_engine_wiring.py` |

If you add a provider, pair it with a unit test for factory wiring. If your extension depends on engine and worker agreeing across the outbox, add an integration test so regressions surface before merge. Full layout: [Testing](testing.md).

When registering custom hooks in a test, reset the registry explicitly — see [Testing → When you add or change code](testing.md#when-you-add-or-change-code).

```bash
uv run pytest tests -q
make check
```

## What's next

With ports and hooks in place, your extension ships through manifests and optional `WARDEN_PLUGINS` — the kernel FSM and outbox stay unchanged. When you add database tables from a plugin, they join the same migration story as the rest of Warden: [Migrations and schema](migrations-and-schema.md). When you change HTTP routes, regenerate API docs per [Testing → Updating API reference docs](testing.md#updating-api-reference-docs).

## Related

- [Architecture](architecture.md) — plugin registry, boot sequence, runtime topology
- [Worker manifests](../guides/manifests/worker-manifests.md) — `provider`, `adapter`, MCP tool sources
- [Testing](testing.md) — suite layout and how to add tests
- [MCP and tools](../guides/manifests/mcp-and-tools.md) — tool sources and at-least-once design
