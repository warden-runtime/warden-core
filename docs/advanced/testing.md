---
sidebar_position: 1
sidebar_label: Testing
pagination_prev: guides/api/recovery
pagination_next: advanced/architecture
---

# Testing

After working through the guides and demos, you'll want confidence that your changes still behave correctly. The test suite is layered on purpose — most runs stay fast on in-memory SQLite, a smaller set exercises engine and worker wiring together, and a Postgres slice locks in SQL and locking behavior SQLite cannot model.

You don't need to run the entire multi-database suite for every minor change. A good workflow is to use targeted `pytest` paths while iterating on your code, and save the full `make tests` validation sweep for right before you open a pull request.

:::note[Contributors]
Extending worker ports or registry hooks? See [Extending Warden](extending-warden.md) for patterns that pair with the test layout here.
:::

## Run the suite

Start with dev dependencies installed, then choose how much you want to run.

```bash
make sync-dev
```

**Full validation** — OSS tests with coverage, including the Postgres slice:

```bash
make tests
```

**Quick pass** — same tests, no coverage report:

```bash
uv run pytest tests -q
```

**Optional live LLM checks** — skipped unless you opt in (does not run under plain `make tests` when `WARDEN_LIVE_LLM` is unset). Uses real provider APIs and costs tokens:

```bash
# Anthropic
WARDEN_LIVE_LLM=1 ANTHROPIC_API_KEY=sk-ant-... \
  uv run --extra worker --extra dev pytest tests/live/test_anthropic_live.py -q -s

# OpenAI
WARDEN_LIVE_LLM=1 OPENAI_API_KEY=sk-... \
  uv run --extra worker --extra dev pytest tests/live/test_openai_live.py -q -s

# Both (each file still skips if its own key is missing)
WARDEN_LIVE_LLM=1 ANTHROPIC_API_KEY=sk-ant-... OPENAI_API_KEY=sk-... \
  uv run --extra worker --extra dev pytest tests/live -q -s
```

Optional model overrides: `WARDEN_ANTHROPIC_MODEL` (default `claude-haiku-4-5-20251001`), `WARDEN_OPENAI_MODEL` (default `gpt-4o-mini`).

**While iterating** — narrow to the area you are changing:

```bash
uv run pytest tests/unit/test_llm_factory.py -q
uv run pytest tests/unit -q
uv run pytest tests/integration -m integration -q
uv run pytest tests/postgres -m postgres -q
```

**Coverage only** (without `make tests`):

```bash
uv run coverage run -m pytest tests -q && uv run coverage report
```

Before opening a PR, run the quality gates as well:

```bash
make check
```

`make check` runs ruff (lint and format), xenon complexity limits on kernel packages, typecheck, and the open-core import boundary check. See [Lint and complexity](#lint-and-complexity) for detail.

### What you need running locally

| Layer | Database | Extra setup |
|-------|----------|-------------|
| `tests/unit/` and `tests/integration/` | In-memory SQLite (automatic) | None — no Compose stack required |
| `tests/postgres/` | Real Postgres | Docker daemon **or** `WARDEN_TEST_POSTGRES_URL` |

For unit and integration tests, `tests/conftest.py` boots an ephemeral SQLite database and resets the plugin registry to NoOp hooks before each test. File-based prompts resolve from `tests/fixtures/prompts` when that directory exists.

For Postgres tests, you do **not** need to start the dev Compose stack manually. When you run `make tests` or `pytest tests/postgres`, the harness tries to start an ephemeral Postgres 16 container via [testcontainers](https://github.com/testcontainers/testcontainers-python) (installed with `make sync-dev`). If Docker is not available, the run fails fast with a clear error rather than silently skipping. Alternatively, point at an existing instance:

```bash
export WARDEN_TEST_POSTGRES_URL=postgres://admin:password@127.0.0.1:5432/engine_db
uv run pytest tests/postgres -m postgres -q
```

Use a disposable database or schema — tests truncate tables between cases (migration tests manage schema directly).

## How the suite is organized

To keep local development fast without sacrificing production reliability, tests are split into layers by speed and environment dependencies:

- **Unit tests (`tests/unit/`)** — Fast, isolated checks in SQLite. Use these for utilities, manifest parsing, FSM helpers, and single-module logic.
- **Integration tests (`tests/integration/`)** — Engine FSM, worker command handling, and outbox rows in one flow (`@pytest.mark.integration`). The LLM/MCP adapter is mocked so nothing hits the network.
- **Postgres tests (`tests/postgres/`)** — Raw SQL, migrations, and transaction locking that SQLite cannot model accurately (`@pytest.mark.postgres`).

| Path | When to look here |
|------|-------------------|
| `tests/unit/` | One module or a small interaction; fast, heavy use of mocks |
| `tests/integration/` | Engine and worker agree on outbox dispatch and saga transitions |
| `tests/postgres/` | Postgres-specific SQL, row locking, migration backfill |
| `tests/conftest.py` | Shared fixtures: asyncio backend, in-memory DB, registry reset |
| `tests/fixtures/` | Prompt templates for saga and worker hydration tests |
| `tests/factories.py` | Helpers to build model instances and payloads |

For a reference integration flow — seed saga rows, process outbox events, assert FSM transitions with the adapter mocked — see `tests/integration/test_worker_engine_wiring.py`.

:::tip[Working with LLMs and MCP in tests]
The suite never calls real inference providers or live MCP servers during unit or integration runs. That keeps tests deterministic and avoids surprise usage bills.

- **LLM provider wiring** — `build_llm(provider="mock", …)` returns `MockChatAdapter`, a scripted responder that parses prompt text and emits predictable tool calls. See `tests/unit/test_llm_factory.py` and `tests/unit/test_llm_mock.py`.
- **Reason-step logic** — pass a small `_ScriptedLLM` (or patch `build_llm`) to exercise the ReAct loop without network I/O. See `tests/unit/test_react_loop.py` and `tests/unit/test_adapters_langchain.py`.
- **Engine ↔ worker wiring** — integration tests patch `workers.logic.resolve_adapter` with a fake adapter whose `run_step` / `run_commit` / `run_compensation` return canned outputs. See `patch_successful_run_step` in `tests/integration/test_worker_engine_wiring.py`.
- **MCP tool plumbing** — unit tests mock MCP sessions and `call_tool` responses rather than opening SSE/stdio transports. See `tests/unit/test_workers_tools_extended.py`.

For a hands-on stack that uses the mock LLM and mock MCP server end-to-end, see [Demo: Mock LLM and MCP](../getting-started/demo-mock-llm-and-mcp.md).
:::

## PostgreSQL tests

Most of the suite never touches a real database. A few kernel paths depend on Postgres primitives — notably `SELECT … FOR UPDATE SKIP LOCKED` in paired outbox reap and migration backfill for `outbox_events.updated_at`. Those tests live in `tests/postgres/` and are included in `make tests`.

| File | What it guards |
|------|----------------|
| `tests/postgres/test_outbox_reap_paired.py` | Paired reap evicts stale claims; concurrent reaps respect `SKIP LOCKED` |
| `tests/postgres/test_recovery_schema_migration.py` | Greenfield `000_initial_schema.sql` includes recovery columns (`updated_at`, `claim_token`, …) |

ORM-level reap and operator recovery are still covered in SQLite under `tests/unit/test_outbox_reap.py` and `tests/unit/test_recovery.py`. Add new Postgres-only behavior under `tests/postgres/`, mark `@pytest.mark.postgres`, and call production helpers rather than duplicating SQL in the test. If a test rebuilds schema, also mark `@pytest.mark.schema_migration` so teardown skips truncate.

## When you add or change code

Match new coverage to the layer you changed — unit first, integration when multiple components interact, Postgres when the behavior is SQL-specific.

| You changed | Add or extend |
|-------------|---------------|
| LLM provider (`build_llm`) | `tests/unit/test_llm_factory.py` |
| Agent adapter (`resolve_adapter`) | `tests/unit/test_agent_adapter.py` |
| Registry hook slot | `tests/unit/test_plugins_registry.py`, `tests/unit/test_engine_hooks.py`, `tests/unit/test_messaging_factory.py` |
| Engine FSM / outbox dispatch | `tests/unit/test_*` near the module, or `tests/integration/test_worker_engine_wiring.py` |
| Outbox reap / row locking / migration SQL | `tests/unit/` for ORM logic; `tests/postgres/` when Postgres primitives are involved |
| Operator recovery (`engine/recovery.py`) | `tests/unit/test_recovery.py` |
| Saga or worker manifest behavior | Unit test beside the validator or resolver; integration if cross-process |

When registering custom registry hooks in a test, reset state explicitly:

```python
from common.plugins import register_engine_hooks, reset_registry

def test_my_hook():
    reset_registry()
    register_engine_hooks(MyHooks())
    # exercise code that calls get_registry().engine...
    reset_registry()
```

The autouse fixture in `conftest.py` resets the registry before and after each test; call `reset_registry()` at the start when your test registers hooks mid-run. Reuse `tests/factories.py` and `tests/fixtures/prompts/` instead of inlining large YAML or prompt bodies.

## Lint and complexity

Complexity limits (xenon) apply to `common/`, `engine/`, `workers/`, and `cli.py`. Refactor rather than raising thresholds when a check fails. Modules like `engine/recovery.py` may legitimately reach grade B — add branch coverage in unit tests when you extend conditional paths.

We enforce strict architectural boundaries to keep the core kernel lightweight. If `make check` reports an import-boundary failure, a kernel package is probably importing an extension module directly instead of going through the plugin registry. See [Architecture → Plugin architecture](architecture.md#plugin-architecture) for how to extend without crossing that line.

## Updating API reference docs

When you modify API routes or request schemas, the OpenAPI specification needs to stay in sync with the code. CI runs `scripts/export_openapi.py --check` before the site build — your PR must include regenerated JSON when routes or schemas change.

Regenerate and preview locally:

```bash
make docs-api
cd website && npm run build
```

Workflow guides live under **Guides → API**; generated reference pages under **API Reference** (`website/sidebars.ts`).

## What's next

When you build on the demos from **Guides**, add or extend tests in the same spirit — unit coverage for validators and helpers, integration when engine and worker must agree, Postgres when locking or migration SQL is involved. [Extending Warden](extending-warden.md) covers registry hooks and worker ports; [Architecture](architecture.md) explains the runtime those tests exercise.

## Related

- [Extending Warden](extending-warden.md) — worker ports, registry hooks, test file pointers
- [Architecture](architecture.md) — plugin registry and runtime topology
- [Migrations and schema](migrations-and-schema.md) — SQL migration apply order and production backfill notes
- [Saga recovery](../guides/cli/saga-recovery.md) — operator recovery runbook exercised by `test_recovery.py`
