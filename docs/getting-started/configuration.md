---
sidebar_position: 8
pagination_prev: getting-started/demo-github-mcp
pagination_next: getting-started/troubleshooting
---

# Configuration

Reference and diagnostic hub for environment variables and on-disk paths: database URLs, `ENGINE_URL`, LLM credentials, `*_ROOT` artifact directories, worker tuning, recovery timeouts, and OpenTelemetry. Use it after the demo arc when wiring production-like settings, and when a demo fails because `.env`, `*_ROOT`, or host vs container paths do not line up — symptom tables on each demo page and [Troubleshooting](troubleshooting.md) are the first stop; this page is the full env catalog and path map.

Copy `.env.example` to `.env` at the repo root and set the variables in the tables below. Process environment variables override `.env` values.

## Runtime context

Warden splits work across processes that may not share the same filesystem view. Under `make up`, engine and worker run in containers while you typically run the CLI on the host.

**Rule of thumb:** The **CLI** needs access to **manifest YAML** (paths you pass to `warden deploy -f …`). The **engine** and **worker** need access to **prompts, policies, schemas, and compensations** via `*_ROOT` inside their process environment—not your host `./config/...` paths unless Compose mounts those files at the container roots below.

### Dev stack (`make up`)

Engine and worker run in containers; you run `warden` on the host.

| Setting | Host (CLI) | Engine / worker (containers) |
|---------|------------|------------------------------|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `.env` — read by Compose `postgres` service | same values bootstrap the DB on first volume init |
| `DB_URL` | `postgres://...@127.0.0.1:5432/engine_db` (host CLI, `make migrate`) | Built by Compose → `...@postgres:5432/...` (**engine + worker**) |
| `ENGINE_URL` | `http://127.0.0.1:8000` (**CLI only**) | not set — engine/worker use Postgres, not HTTP to each other |
| `PROMPTS_ROOT` | **Leave unset** in `.env` | `/app/prompts` (set in `docker-compose.yml`) |
| `POLICIES_ROOT` | **Leave unset** in `.env` | `/app/policies` |
| `SCHEMAS_ROOT` | **Leave unset** in `.env` | `/app/schemas` |
| `COMPENSATIONS_ROOT` | **Leave unset** in `.env` | `/app/compensations` |

Compose mounts your repo's `./config/` tree into each container. The host path and container path are different names for the same files:

| Repo path (host disk) | Mount inside container | Read by |
|-----------------------|------------------------|---------|
| `./config/prompts/` | `/app/prompts` | engine (register + validate), worker (execute) |
| `./config/policies/` | `/app/policies` | engine |
| `./config/schemas/` | `/app/schemas` | engine |
| `./config/compensations/` | `/app/compensations` | engine |

:::warning[Do not mix host paths into `.env` for Compose]
If you set `PROMPTS_ROOT=./config/prompts` in `.env`, Compose injects that value into containers via `env_file` — but `./config/prompts` does not exist *inside* the container filesystem. The compose file already sets `PROMPTS_ROOT=/app/prompts` and mounts `./config/prompts` there. **Leave `PROMPTS_ROOT`, `POLICIES_ROOT`, `SCHEMAS_ROOT`, and `COMPENSATIONS_ROOT` unset in `.env` for standard `make up` workflows** so containers use those compose-defined paths instead of a host-relative path that does not resolve inside the image.
:::

`warden deploy -f config/saga.minimal.yaml` reads YAML from your host working tree. After deploy, manifest bodies live in Postgres; prompt, policy, schema, and compensation **files** stay on disk and must be visible at the container `*_ROOT` paths above.

### Minimal template (`docker-compose.example.yml`)

Same host `DB_URL`, in-compose `postgres:5432`, and container `*_ROOT` paths as the dev stack. See [Installation → Local development stack](installation.md#local-development-stack).

### Variable quick reference

| Variable | Host CLI | In containers |
|----------|----------|---------------|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `.env` for Compose substitution | `postgres` service bootstrap |
| `DB_URL` | `127.0.0.1:5432` | `postgres:5432` (**engine + worker**) |
| `PROMPTS_ROOT` | leave unset in `.env` | `/app/prompts` |
| `POLICIES_ROOT` | leave unset in `.env` | `/app/policies` |
| `SCHEMAS_ROOT` | leave unset in `.env` | `/app/schemas` |
| `COMPENSATIONS_ROOT` | leave unset in `.env` | `/app/compensations` |
| `ENGINE_URL` | `http://127.0.0.1:8000` (CLI only) | not set |

### Database (Compose)

Docker Compose uses two related sets of database variables:

| Variables | Read by | Purpose |
|-----------|---------|---------|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `postgres` service | Bootstrap the database and role on first container start |
| `DB_URL` in `.env` | Host CLI; optional `make migrate` on the host | Reach Postgres via the published port `127.0.0.1:5432` |

Compose **overrides** `DB_URL` for engine, worker, and migrate — those containers connect to `postgres:5432` on the internal network. You do not set their `DB_URL` in `.env`.

**Both the engine and the worker require `DB_URL`.** The outbox is Postgres-native: the engine writes commands and ingests results; the worker claims work and reports `STEP_COMPLETED` / `STEP_FAILED`. Each process must reach the same database—host CLI tools use `127.0.0.1:5432`; in Compose, engine and worker use the internal `postgres:5432` hostname.

Keep `POSTGRES_*` and host `DB_URL` aligned (same user, password, and database name). You can use any values — `admin` / `password` / `engine_db` are the repo defaults, not a hard requirement:

```bash
POSTGRES_DB=engine_db
POSTGRES_USER=admin
POSTGRES_PASSWORD=password
DB_URL=postgres://admin:password@127.0.0.1:5432/engine_db
```

`POSTGRES_*` is applied only when the Postgres data volume is created. Changing credentials in `.env` later does not update an existing volume — run `make reset` or alter the role manually.

Use `127.0.0.1` in host `DB_URL`, not `postgres` — the service hostname resolves only inside Compose.

## Dev stack (Makefile and ports)

When you use `make up`, Postgres data persists in the Docker named volume `engine_db_data`. Stopping containers keeps that volume; deleting it gives you an empty database on the next start.

### Make targets

| Target | Effect |
|--------|--------|
| `make up` | Start dev compose (db, migrate, engine, worker, jaeger, adminer) |
| `make up-db` | Postgres only |
| `make stop` / `make down` | Stop containers; keep `engine_db_data` |
| `make clean` | Stop and delete `engine_db_data` |
| `make reset` | `make clean` then `make up` |
| `make migrate` | Apply migrations from the host (`DB_URL` → `127.0.0.1:5432`) |
| `make doctor` | Service status + recent migrate, engine, and worker logs |

You normally do not run `make migrate` for a first boot — the one-shot **migrate** service runs before engine and worker start. Use host-side `make migrate` when Postgres is up but you are not using the full compose migrate container. If the engine or worker exits with `Database schema is not initialized`, migrations did not complete — see [Troubleshooting](troubleshooting.md).

### Default ports (dev stack)

| Service | URL |
|---------|-----|
| Engine | http://127.0.0.1:8000 |
| Postgres (from host) | 127.0.0.1:5432 |
| Adminer | http://127.0.0.1:8080 |
| Jaeger | http://127.0.0.1:16686 |

## Environment variables

| Variable | Consumer | Notes |
|----------|----------|-------|
| `DB_URL` | **engine**, **worker**, migrations | **Both engine and worker** need Postgres for the outbox loop. Host: `127.0.0.1:5432`. Compose containers: built from `POSTGRES_*` → `postgres:5432` |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `postgres` service (Compose) | Must match credentials in host `DB_URL` |
| `ENGINE_URL` | **host CLI only** | `http://127.0.0.1:8000` from your machine (published port). Engine and worker containers do **not** use this variable—they coordinate through Postgres. If you run the CLI inside the Compose network, use `http://engine:8000` instead of loopback |
| `OPENAI_API_KEY` | worker | when `provider: openai` — not required for `provider: local` or `provider: mock`. Checked at **step runtime**, not at deploy |
| `ANTHROPIC_API_KEY` | worker | when `provider: anthropic` — Claude via LangChain. Checked at **step runtime**, not at deploy |
| `LOGGING_LEVEL` | engine, worker | Root JSON log level (`INFO` default; use `DEBUG` for ReAct transcript lines on `warden.react.transcript`) |
| `WARDEN_LOCAL_LLM_BASE_URL` | worker | OpenAI-compatible local endpoint (optional; defaults to `http://localhost:11434/v1`) |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | worker | GitHub MCP demo only (stdio `env_inherit`) |
| `${ENV:…}` in worker manifest | worker | SSE MCP auth — names like `COMPANY_MCP_TOKEN` referenced in `tool_sources[].headers`; set on worker, not in YAML |

Under Compose, `.env` is injected when a container **starts**. After you add or change worker-scoped variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `WARDEN_LOCAL_LLM_BASE_URL`, MCP tokens), restart the worker — not the engine: `docker compose up -d worker`. A step that already failed with missing credentials stays failed until you retry it or start a new saga instance.

For **hosted SSE MCP**, declare headers in the worker manifest with `${ENV:VAR}` placeholders and set `VAR` on the worker process. Example worker manifest:

```yaml
headers:
  Authorization: "Bearer ${ENV:COMPANY_MCP_TOKEN}"
```

```bash
# .env (worker container via env_file)
COMPANY_MCP_TOKEN=your-bearer-token
```

See [MCP and tools → Hosted MCP authentication](../guides/manifests/mcp-and-tools.md#hosted-mcp-authentication-sse) and [Worker manifests](../guides/manifests/worker-manifests.md#mcp-tool-sources).

A minimal `.env` for Compose + host CLI:

```bash
POSTGRES_DB=engine_db
POSTGRES_USER=admin
POSTGRES_PASSWORD=password
DB_URL=postgres://admin:password@127.0.0.1:5432/engine_db
ENGINE_URL=http://127.0.0.1:8000
OPENAI_API_KEY=sk-...
```

For the **local Quickstart path**, set `WARDEN_LOCAL_LLM_BASE_URL` and deploy `config/worker.local-minimal.yaml`:

```bash
WARDEN_LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
```

For the **mock demo**, omit both — use `provider: mock` manifests.

### Local LLM under Docker (Ollama)

Warden does **not** run Ollama in Compose. You install and start Ollama on the **host** (`systemctl start ollama` or the desktop app), pull the model named in your worker manifest (`ollama pull llama3.2`), then point the **worker container** at that HTTP API.

Two common gotchas when the worker runs in Docker but Ollama runs on the host:

1. **Ollama binds to loopback only** — default is `127.0.0.1:11434`, which containers cannot reach. Set `OLLAMA_HOST` on the Ollama service so it accepts traffic from the Docker bridge, then restart Ollama:

   ```bash
   sudo mkdir -p /etc/systemd/system/ollama.service.d
   sudo tee /etc/systemd/system/ollama.service.d/override.conf <<'EOF'
   [Service]
   Environment="OLLAMA_HOST=0.0.0.0:11434"
   EOF
   sudo systemctl daemon-reload
   sudo systemctl restart ollama
   ```

   Confirm with `ss -tlnp | grep 11434` — you want `0.0.0.0:11434`, not only `127.0.0.1`. Binding to all interfaces is fine for local dev; restrict port `11434` with a firewall if the machine is on an untrusted network.

2. **`host.docker.internal` on Linux** — Docker Desktop defines this hostname automatically; native Linux Docker often does not. The repo's `docker-compose.yml` (and `docker-compose.example.yml`) add `extra_hosts` on the **worker** service:

   ```yaml
   extra_hosts:
     - "host.docker.internal:host-gateway"
   ```

   Recreate the worker after adding it: `docker compose up -d worker`. If you use a custom Compose file without `extra_hosts`, set `WARDEN_LOCAL_LLM_BASE_URL` to your Docker bridge gateway instead (for example `http://172.18.0.1:11434/v1` — inspect with `docker inspect <worker-container> --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'`).

**Worker manifest:** `provider: local` and `model_name` matching `ollama list` — see `config/worker.local-minimal.yaml`.

**Smoke tests:**

```bash
# Host — Ollama API up
curl http://127.0.0.1:11434/v1/models

# Worker container — DNS + HTTP to host Ollama
docker compose exec worker python -c "
import socket, urllib.request
print(socket.gethostbyname('host.docker.internal'))
print(urllib.request.urlopen('http://host.docker.internal:11434/v1/models', timeout=5).read()[:200])
"
```

After changing `.env` or Compose networking, restart the worker (`docker compose up -d worker`). Failed saga steps stay `FAILED` until you retry or start a new instance — see [Demo: Quickstart → When a step shows `FAILED`](demo-quickstart.md#when-a-step-shows-failed).

## Disk artifact roots

Worker and saga **manifest YAML** is stored in Postgres when you `warden deploy`. Prompts, policies, output schemas, and compensation files stay on disk and are resolved at runtime using these variables:

| Variable | Resolves | Consumer |
|----------|----------|----------|
| `PROMPTS_ROOT` | `prompt: foo.j2` → `{root}/foo.j2` | engine (register), worker (execute) |
| `POLICIES_ROOT` | `policy: gate.yaml` → `{root}/gate.yaml` (legacy stem `gate` → `{root}/gate.yaml`) | engine |
| `SCHEMAS_ROOT` | `output_schema: triage.json` → `{root}/triage.json` | engine (register + saga start) |
| `COMPENSATIONS_ROOT` | `compensation: disburse_undo.yaml` → `{root}/disburse_undo.yaml` | engine (register + saga start) |

Each value is a path relative to the root — subdirectories are allowed (e.g. `policy: teams/marketing/gate.yaml`). For `policy`, prefer an explicit path with extension; stem-only refs without an extension still resolve via `{ref}.yaml` when the exact path is missing (one deploy-time warning per unique legacy ref).

Repo defaults are `./config/prompts`, `./config/policies`, `./config/schemas`, and `./config/compensations`.

## Worker tuning

These default to conservative values. Adjust as needed for your workload.

ReAct tool responses can be large. `WARDEN_REACT_TOOL_MESSAGE_LIMIT` trades **token economy** against **context visibility** in the LLM transcript: a lower limit saves context window but clips the tail of tool payloads—the agent may miss data and reason from incomplete context. Set `0` to disable clipping entirely for deep debugging passes. Facts extraction always uses the full tool payload regardless of this limit.

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKER_MAX_IN_FLIGHT` | `1` | Max concurrent step commands handled by **one worker process** (outbox consumer semaphore) |
| `WARDEN_REACT_TOOL_MESSAGE_LIMIT` | `8000` | Max characters for tool-role messages in the ReAct LLM transcript; `0` disables clipping |
| `WARDEN_MAX_STEP_TOKENS` | unset / `0` | Process-wide fallback token budget for reason steps that omit `max_step_tokens`; `0` or unset = no fallback |

### LLM JSON admission

Before strict JSON Schema validation, the worker **admits** sloppy LLM-authored JSON against the declared schema. This is always on (no env var) and helps local OpenAI-compatible models (Ollama, vLLM) that often emit stringified JSON where the schema expects typed values — for example `commands: '["echo ok"]'` when a tool declares `commands` as an `array`, or `feasible: "false"` when `output_schema` requires a boolean.

Applies to:

- MCP tool arguments (against each tool's `inputSchema`) on **`react`** steps
- Reason-step **`output_schema`** for `_submit` payloads (**`react`**) and structured completion (**`simple`**)

Does **not** apply to commit-step tool `output.data` (MCP/server JSON) or non-LLM governance tool inputs.

| Behavior | Detail |
|----------|--------|
| Depth | Two levels: top-level fields, plus one nested level inside arrays and objects |
| Coercions | Stringified JSON arrays/objects; scalar strings → `integer`, `number`, or `boolean` when unambiguous |
| Nullable unions | Union `type` arrays (e.g. `["string", "null"]`) supported; string `"null"` / `"none"` (case-insensitive, trimmed) coerces to JSON `null` when the schema allows it |
| `string` fields | Never JSON-parsed (a string value that looks like JSON stays a string) |
| Best-effort | Values that cannot be coerced are left unchanged; validation may still fail downstream |
| Limitation | Schemas deeper than two levels are not recursively coerced; deeply nested mistakes may still fail |

Governance audit hashes for MCP tool calls record the **raw** LLM tool args, not the admitted values passed to MCP. Admitted reason-step payloads are what land in saga `output.data`.

### Throughput and parallelism

Parallelism happens at the **worker** level, not inside a single saga. The engine still schedules **one forward step at a time** per saga instance—steps in the same workflow do not run concurrently. Throughput across **many** saga instances comes from how many worker commands your fleet can execute at once.

Each **worker process** (typically one `workers/main.py` per container) polls the `worker-commands` outbox and runs handlers behind an asyncio semaphore sized by `WORKER_MAX_IN_FLIGHT`:

| Scale lever | Effect |
|-------------|--------|
| Raise `WORKER_MAX_IN_FLIGHT` on one process | That process may execute multiple step commands concurrently (different sagas, or different steps that are already queued) |
| Run more worker replicas | More processes compete for outbox rows—each replica usually keeps `WORKER_MAX_IN_FLIGHT=1` unless you have a reason to combine both |

Example: four worker containers with default `WORKER_MAX_IN_FLIGHT=1` can execute up to four step commands at once, usually for four different saga instances. One container with `WORKER_MAX_IN_FLIGHT=4` can do the same from a single process, at the cost of more concurrent LLM/MCP load and DB connections in that process.

If you raise `WORKER_MAX_IN_FLIGHT` materially (for example above `8`), size the worker Postgres pool accordingly.

## Recovery timeouts {/* #recovery-timeouts */}

Background loops in the worker and engine processes recover from worker crashes and outbox consumer stalls. Set these timeouts **above** worst-case step latency (LLM + MCP) for your manifests.

| Variable | Default | Consumer | Purpose |
|----------|---------|----------|---------|
| `WORKER_STALE_CLAIM_SECONDS` | `1800` | worker | Reap unfinished `processed_commands` claims |
| `WORKER_CLAIM_REAP_INTERVAL_SECONDS` | `60` | worker | Claim reap tick interval |
| `OUTBOX_STALE_IN_PROGRESS_SECONDS` | `1800` | engine, worker | Reap outbox rows stuck `IN_PROGRESS` |
| `OUTBOX_REAP_INTERVAL_SECONDS` | `60` | engine, worker | Outbox reap tick interval |
| `OUTBOX_REAP_BATCH_SIZE` | `20` | engine, worker | Max rows reaped per tick per topic |

Workers use `claim_token` fencing: superseded handlers log `claim_superseded` with `execution_duration_s` instead of emitting duplicate results. Frequent supersession within a few seconds means these timeouts are too low.

Operator recovery (after automation): `warden saga retry-step` / `warden saga retry-compensation`. **`--force` on commit steps requires `--allow-destructive`** (duplicate side-effect risk). See [Saga recovery](../guides/cli/saga-recovery.md).

## Observability (OpenTelemetry)

Engine and worker export **traces** over OTLP gRPC at startup. Warden reads these settings from the environment:

| Variable | Default | Consumer | Purpose |
|----------|---------|----------|---------|
| `OTLP_ENDPOINT` | unset | engine, worker | Collector URL (for example `http://127.0.0.1:4317` or `http://jaeger:4317` in Compose) |
| `OTLP_INSECURE` | `true` | engine, worker | Plaintext gRPC when `true`; set `false` for TLS |

When `OTLP_ENDPOINT` is unset, the OpenTelemetry SDK may still honor `OTEL_EXPORTER_OTLP_ENDPOINT` and other standard `OTEL_EXPORTER_OTLP_*` variables on the process.

With **`make up`**, Compose already points engine and worker at Jaeger (`OTLP_ENDPOINT=http://jaeger:4317`). Open the UI at [http://127.0.0.1:16686](http://127.0.0.1:16686) and filter traces on attribute `saga.id` (your Warden `trace_id` from `warden start saga`).

For correlation fields, TLS, vendor headers, and debugging workflows, see [Observability](../guides/observability.md).

:::info[Enterprise tier]
The open-core kernel ships engine, workers, CEL policies, HITL gates, compensation, and Postgres-backed saga state—enough to build and run governed workflows in your own infrastructure.

For compliance-grade capabilities—forensic audit history, extended operational enforcement, and related enterprise features—see [Open Core vs Enterprise](open-core-vs-enterprise.md).
:::

## LLM retries (automated backoff)

`WARDEN_LLM_RETRY_*` variables configure **transient LLM API resilience** inside the worker — network blips, rate limits, and short provider outages on each `ainvoke` call (ReAct turns on **`react`** steps, the single call on **`simple`** steps). The worker retries with exponential backoff and jitter; when the provider suggests a wait (`Retry-After` or “Please try again in Xs”), the sleep is **at least** that duration (still capped by `WARDEN_LLM_RETRY_MAX_DELAY_S`). It does **not** restart a failed saga step, re-run compensation, or replace operator actions.

| Variable | Default | Purpose |
|----------|---------|---------|
| `WARDEN_LLM_RETRY_ENABLED` | `true` | Toggle backoff wrapper around LLM calls |
| `WARDEN_LLM_RETRY_MAX_ATTEMPTS` | `3` | Max attempts per LLM call (including the first) |
| `WARDEN_LLM_RETRY_BASE_DELAY_S` | `1.0` | Initial backoff delay (seconds) |
| `WARDEN_LLM_RETRY_MAX_DELAY_S` | `60.0` | Hard cap on sleep (backoff and provider wait hints) |

| If you need… | Use… |
|--------------|------|
| Backoff on a transient provider error during execution | `WARDEN_LLM_RETRY_*` (above) |
| An operator to re-run a paused human-gated step | `warden review retry` — see [HITL review](../guides/cli/hitl-review.md) |
| Recovery after a forward step is stuck `IN_PROGRESS` | `warden saga retry-step` — see [Saga recovery](../guides/cli/saga-recovery.md) |
| Recovery after compensation failed or stalled | `warden saga retry-compensation` |
| Recovery after the step has already failed (normal path) | Saga FSM and compensation — no env var auto-restarts the step |

## What's next

Path mismatches between host and container are the most common deploy failure—start with [Runtime context](#runtime-context) when `./config/...` works on the host but registration or execution fails inside Docker. For active errors, see [Troubleshooting](troubleshooting.md).

For saga authoring, MCP wiring, and policy design, continue to [Guides → Manifests and artifacts](../guides/manifests/overview.md). For a lean deployment layout without dev-only services, start from `docker-compose.example.yml` in the repo root.

## Related

- [Installation](installation.md)
- [Troubleshooting](troubleshooting.md)
- [Observability](../guides/observability.md)
