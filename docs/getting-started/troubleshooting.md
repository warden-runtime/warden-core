---
sidebar_position: 8
pagination_prev: getting-started/configuration
pagination_next: getting-started/open-core-vs-enterprise
---

# Troubleshooting

When a hands-on demo fails, start with the **Operational diagnostics** table on that demo page — those rows cover the symptoms you hit during the walkthrough.

| Demo | First-line fixes |
|------|------------------|
| [Demo: Mock LLM and MCP](demo-mock-llm-and-mcp.md) | [Operational diagnostics](demo-mock-llm-and-mcp.md#operational-diagnostics) — `ENGINE_URL`, allowlist, MCP subprocess |
| [Demo: Quickstart](demo-quickstart.md) | [When a step shows `FAILED`](demo-quickstart.md#when-a-step-shows-failed) — `OPENAI_API_KEY`, local model URL, worker logs |

Use this page when those tables do not resolve the issue, or when the failure happens before you reach a demo (install, deploy, or engine health).

## Local stack diagnostics

When containers look unhealthy after [Installation](installation.md), start here before diving into saga-level errors.

```bash
make doctor
```

`make doctor` prints `docker compose ps` and recent logs from migrate, engine, and worker — enough to spot a failed migration, crash loop, or port conflict.

| Symptom | What to try |
|---------|-------------|
| Added LLM or MCP credentials to `.env` but steps still fail with missing key / connection errors | Restart the **worker** (`docker compose up -d worker`) — not the engine. Compose injects `.env` at container start. Retry the failed step or start a new saga. |
| Local Ollama works on the host (`curl localhost:11434`) but worker steps fail with connection errors | Ollama may bind only `127.0.0.1`, or `host.docker.internal` may not resolve in the worker container on Linux — [Configuration → Local LLM under Docker (Ollama)](configuration.md#local-llm-under-docker-ollama) |
| Stale schema or bad credentials after editing `.env` | `make reset` (wipes `engine_db_data` and runs `make up`) — only when you can lose local DB state |
| Stop stack, keep data | `make down` |
| Wipe data without full restart sequence | `make clean`, then `make up` |
| Minimal template, migration failed | `docker compose -f docker-compose.example.yml logs migrate` |

[Adminer](http://127.0.0.1:8080) browses Postgres on the dev stack. Default ports and Makefile targets are in [Configuration → Dev stack](configuration.md#dev-stack-makefile-and-ports). Prefer `warden list sagas --trace-id …` and `warden list steps --trace-id …` for saga state before opening the database UI.

## Diagnose the failure mode

Most infrastructure failures fall into one of two buckets:

- **Network boundary** — the CLI cannot reach the engine API (`ENGINE_URL`, health checks, published ports).
- **Outbox / worker execution** — the engine wrote commands to Postgres, but the worker is not claiming or finishing them (crash loop, stale claim, step stuck `IN_PROGRESS`).

Use **Stack and CLI** for the first; **Runtime** for the second. **Deploy and start** covers validation before any instance runs.

## Stack and CLI (network boundary) \{#stack-and-cli\}

| What you see | Likely cause | What to do |
|--------------|--------------|------------|
| `bash: warden: command not found` | CLI not on shell `PATH` | Run `make sync-dev`, then `source .venv/bin/activate` or prefix with `uv run` (e.g. `uv run warden ping`) — see [Installation](installation.md) |
| `ERROR ENGINE_URL is required …` | `ENGINE_URL` unset | Set per [Configuration](configuration.md) |
| `GET /v1/health failed: … connection refused` | Engine not running | `make up`; `docker compose ps`; confirm port `8000` |
| `GET /v1/health failed: … timed out` | Engine overloaded or blocked | `make doctor`; retry when healthy |
| Engine/worker exits: `Database schema is not initialized` | Migrations did not run | `make doctor` or `docker compose logs migrate` — [Local stack diagnostics](#local-stack-diagnostics) |

## Deploy and start

`warden deploy` validates YAML, worker references, and prompt files on disk. It does **not** check LLM credentials — missing keys surface at step runtime on [Demo: Quickstart](demo-quickstart.md).

| What you see | Likely cause | What to do |
|--------------|--------------|------------|
| `ERROR file not found: config/…` | Wrong `-f` path | Run from repo root |
| `… workers that are not registered …` | Saga deployed before worker | Register the worker manifest first — see [Demo: Mock LLM and MCP](demo-mock-llm-and-mcp.md#manual-cli-walkthrough) |
| `… prompt is invalid: Prompt file not found …` | Missing template or wrong `PROMPTS_ROOT` on engine | Mount `./config/prompts`; see [Configuration](configuration.md) |
| `SagaDefinition not found …` on start | Definition not deployed, or `(namespace, name, version)` mismatch | Redeploy; match `-n`, `-v`, and `--namespace` to manifest fields (omit `--namespace` only when manifest uses `default`) |

## Runtime (outbox and worker)

| What you see | Likely cause | What to do |
|--------------|--------------|------------|
| Saga `RUNNING`; step stuck `IN_PROGRESS` | Worker down or orphaned claim | `make doctor`; [Saga recovery](../guides/cli/saga-recovery.md) after recovery timeouts |
| Saga `FAILED` shortly after start | Worker returned `STEP_FAILED` | `warden list steps --trace-id …` — failed rows show `FAILED*`; add `--errors` for one-line briefs or `warden show step …` for full `error_details` |
| `TOOL_INVOKE_FAILED` with Pydantic `list_type` / `dict_type` on a local model | Model emitted stringified JSON for an array/object field, or args are wrong past the coercion depth | Warden admits common cases automatically ([Configuration → LLM JSON admission](configuration.md#llm-json-admission)); if it still fails, inspect `error_details.message`, confirm MCP `inputSchema` types, and check for mistakes deeper than two nesting levels |
| `validation: output_schema` / `OUTPUT_SCHEMA_VALIDATION_FAILED` with stringified booleans/arrays on a local model | `_submit` or `simple` structured output used `"false"` / `'["a"]'` where the schema expects typed values | Same admission layer coerces common cases before validation ([LLM JSON admission](configuration.md#llm-json-admission)); remaining failures usually mean wrong keys, missing required fields, or uncoerceable values |
| Empty `list sagas` but you started one | **Namespace filter mismatch** — instances are isolated by namespace; a list query with the wrong filter returns nothing, not a missing saga | Pass the same `--namespace` used at `warden start saga` (default `default`), or pin `--trace-id` |

For MCP tool failures (GitHub demo, hosted SSE, stdio auth), see [MCP and tools](../guides/manifests/mcp-and-tools.md#transport-sse-vs-stdio). Mock MCP subprocess issues are covered in [Demo: Mock LLM and MCP → Operational diagnostics](demo-mock-llm-and-mcp.md#operational-diagnostics). Reason-step completion errors (`no_submit_call`, `structured_output_failed`, etc.) are listed in [Saga manifests → Reason step execution](../guides/manifests/saga-manifests.md#reason-step-execution-agent-adapter) and [Demo: Quickstart → When a step shows `FAILED`](demo-quickstart.md#when-a-step-shows-failed).

## What's next

If demo tables and the sections above did not resolve the issue, continue with [Saga recovery](../guides/cli/saga-recovery.md) for stuck `IN_PROGRESS` steps and operator retries (`warden saga retry-step`, `warden saga retry-compensation`). Cross-check [Configuration → Recovery timeouts](configuration.md#recovery-timeouts) when claims or outbox rows stay stale after worker restarts.

## Related

- [Configuration](configuration.md) — env reference and recovery timeouts
- [Saga recovery](../guides/cli/saga-recovery.md) — operator retry when steps stay `IN_PROGRESS`
- [Open Core vs Enterprise](open-core-vs-enterprise.md)
