# warden-mcp

MCP server that exposes the [Warden](https://warden-runtime.org) engine control-plane API to Cursor, Claude Desktop, and other MCP clients.

This package lives in the **warden-core** repo under [`mcp/`](.) as a **separate** installable distribution (`warden-mcp`). It is a thin HTTP adapter: MCP tools call the engine at `ENGINE_URL`. It does not connect to Postgres, import `common` / `engine` / `workers` / `cli`, or run saga logic.

Compatible with the engine API from **this repository revision** (shipped with core git tags; package version may bump independently when tools change).

## Boundary rules

1. `warden_mcp` **must not** import `common`, `engine`, `workers`, or `cli`.
2. New engine semantics belong in `engine/api` first; MCP only wraps HTTP.
3. Do not share Tortoise models or registry code — keep the process boundary clean.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A running Warden engine (repo root: `make up`)

## Setup

From the **warden-core** repo root:

```bash
make sync-dev
# or: uv sync --all-packages --all-extras
cp mcp/.env.example mcp/.env   # optional; ENGINE_URL defaults to http://127.0.0.1:8000
```

Do not run `uv sync --directory mcp` — that makes the shared root `.venv` reflect only `warden-mcp` and uninstalls `warden`.

## Run

**stdio** (default — for Cursor / Claude Desktop):

```bash
uv run --directory mcp warden-mcp
```

`--directory mcp` sets the process working directory (so `mcp/.env` loads). It does not create a separate virtualenv; the server uses the root `.venv` from `make sync-dev`.

**Streamable HTTP** (for remote clients or worker `tool_sources`):

```bash
WARDEN_MCP_TRANSPORT=streamable-http uv run --directory mcp warden-mcp
# listens on http://127.0.0.1:8765/mcp/
```

Or via FastMCP CLI:

```bash
uv run --directory mcp fastmcp run src/warden_mcp/server.py:mcp --transport stdio
```

## Cursor configuration

Add to `.cursor/mcp.json` (or global MCP settings); point `--directory` at this repo’s `mcp/` folder:

```json
{
  "mcpServers": {
    "warden": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/warden-core/mcp", "warden-mcp"],
      "env": {
        "ENGINE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

For Streamable HTTP instead:

```json
{
  "mcpServers": {
    "warden": {
      "url": "http://127.0.0.1:8765/mcp/"
    }
  }
}
```

## Tools

| Tool | Engine route |
|------|--------------|
| `warden_ping` | `GET /v1/health` (liveness) |
| `warden_ready` | `GET /v1/health/ready` (Postgres) |
| `warden_deploy_manifest` | `POST /v1/manifests` (`kind: worker` \| `step` \| `saga`; `dry_run` query) |
| `warden_list_saga_definitions` | `GET /v1/definitions/sagas` |
| `warden_list_worker_definitions` | `GET /v1/definitions/workers` |
| `warden_list_step_definitions` | `GET /v1/definitions/steps` (catalog) |
| `warden_get_saga_definition` | `GET /v1/definitions/sagas/{id}` |
| `warden_get_worker_definition` | `GET /v1/definitions/workers/{id}` |
| `warden_get_step_definition` | `GET /v1/definitions/steps/{id}` (catalog) |
| `warden_set_definition_active` | `PATCH /v1/definitions/{workers\|steps\|sagas}?…` |
| `warden_start_saga` | `POST /v1/sagas/start` |
| `warden_list_sagas` | `GET /v1/sagas` (filters: `in_flight`, `failed`, `status`, …) |
| `warden_get_saga_status` | `GET /v1/sagas?trace_id=...` (single-instance shape) |
| `warden_list_saga_steps` | `GET /v1/sagas/steps` (**runtime** instances) |
| `warden_get_step_detail` | `GET /v1/sagas/{trace_id}/steps/{step_span_id}` (**runtime**) |
| `warden_wait_for_saga` | Polls until terminal/HITL/timeout; includes slim `steps` summary |
| `warden_list_pending_reviews` | `GET /v1/sagas/pending-review` |
| `warden_decide_step` | `POST .../decision` |
| `warden_approve_step` | `POST .../approve` |
| `warden_reject_step` | `POST .../reject` |
| `warden_retry_hitl_step` | `POST .../retry` |
| `warden_retry_stuck_step` | `POST .../retry-step` |
| `warden_retry_compensation` | `POST .../retry-compensation` |

**Catalog vs runtime:** `*_step_definitions` tools inspect reusable `kind: step` manifests. `warden_list_saga_steps` / `warden_get_step_detail` inspect instances on a running saga.

**Deploy order:** workers → steps → sagas. Saga YAMLs compose catalog steps with `use:` / `version`; deploy referenced workers and steps first. Pass `dry_run=true` on `warden_deploy_manifest` to validate/link-check without persisting (same as `warden deploy --dry-run`).

Prefer `warden_ready` before deploy/start. Saga mutations return **202 Accepted** and enqueue work asynchronously; poll status (or `warden_wait_for_saga`). Manifest deploy remains **200** synchronous.

## Breaking changes (recent)

- **`warden_start_saga`**: parameter `input` renamed to **`saga_input`**; response includes engine `created` when present.
- **`warden_get_saga_status`**: returns `{found, trace_id, saga}` only (no duplicated list pagination fields).
- **`warden_wait_for_saga`**: default timeout reduced to **60s**; returns immediately when `trace_id` is unknown.
- **`ENGINE_URL`**: also accepted as **`WARDEN_MCP_ENGINE_URL`**.
- **First-class steps:** deploy `kind: step`; list/get via `warden_*_step_definitions`; soft-disable via `warden_set_definition_active` (query `id` XOR `namespace+name+version`, not path UUID).
- **Saga `include_body`:** authoring AST only; hydration runs at start.
- **Catalog HTTP errors:** tool payloads may include `code`, `catalog_kind`, `namespace`, `name`, `version` (e.g. `CATALOG_DEFINITION_NOT_FOUND` / `INACTIVE_CATALOG_DEFINITION`).

## Development

From repo root:

```bash
make test-mcp
# or:
uv run ruff check mcp/src mcp/tests
uv run ruff format mcp/src mcp/tests
uv run --package warden-mcp pytest mcp/tests
```

## Architecture

```text
MCP client (Cursor)
       │ stdio or streamable-http
       ▼
  warden-mcp (FastMCP)   ← mcp/ in this repo
       │ httpx
       ▼
  Warden engine :8000 (/v1/*)
       │
       ▼
    Postgres
```

See [warden-core API guides](https://warden-runtime.org/docs/guides/api/overview) for HTTP semantics.
