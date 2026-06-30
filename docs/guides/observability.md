---
sidebar_position: 4
pagination_prev: guides/manifests/compensation
pagination_next: guides/cli/overview
---

# Observability

When a saga runs, state lands in Postgres and spans land in your OTLP collector — if you configure one. Both paths share the same Warden `trace_id`, so you can move between SQL and traces without guessing which run you are looking at.

Postgres is always available on the dev stack and is the system of record. OpenTelemetry is optional: set `OTLP_ENDPOINT` when you want distributed traces in Jaeger, Tempo, Honeycomb, or similar. The sections below start with Postgres diagnostics (status, stuck steps, timing JSON), then OTLP setup and how Warden correlates spans to saga rows.

## Postgres

Postgres is the system of record. Every saga and step instance is a row — not an in-memory event you have to catch in time.

The primary tables:

| Table | What it holds |
|-------|---------------|
| `saga_instances` | One row per saga execution — status, `trace_id`, start/end times |
| `saga_step_instances` | One row per step execution — status, outputs, errors |
| `outbox_events` | Transactional outbox — dispatched work and HITL decisions |

The `trace_id` is the correlation key. It's the 32-character hex ID returned by `warden start saga`. Use `warden list sagas --trace-id …` for saga status and `warden list steps --trace-id … --json` for per-step rows (includes `error_details` on failures). HTTP equivalents: `GET /v1/sagas?trace_id=…` and `GET /v1/sagas/steps?trace_id=…` — see [Start and monitor](api/start-and-monitor.md). In SQL, join `saga_step_instances` on `saga_trace_id`.

For ad-hoc SQL or Adminer on the dev stack, query the tables directly. See [Start and monitor](cli/start-and-monitor.md).

### Stuck or stranded steps

If a step stays `IN_PROGRESS` with no worker progress, do **not** patch `saga_step_instances` directly — updating rows in SQL bypasses the engine FSM and will not enqueue compensation or emit outbox events.

| Symptom | First checks | Recovery path |
|---------|--------------|---------------|
| `IN_PROGRESS`, worker crashed mid-command | Worker logs for `trace_id`; `processed_commands` idempotency row | Wait for claim reap (`WORKER_STALE_CLAIM_SECONDS`, default 1800s) to clear stale claims and redeliver the outbox command |
| `COMPENSATING` with failed undo step | Worker logs for `EXECUTE_COMPENSATION` | Fix environment; see [Saga recovery](cli/saga-recovery.md) |

```sql
-- Diagnose: steps that look stranded
SELECT saga_trace_id, span_id, step_id, status, started_at, error_details
FROM saga_step_instances
WHERE status = 'IN_PROGRESS'
ORDER BY started_at;
```

Full runbook: [Saga recovery](cli/saga-recovery.md).

## OpenTelemetry

Engine and worker export spans when OTLP is configured at startup. The default exporter uses **OTLP gRPC** (port **4317**).

### OTLP endpoint setup

| Variable | Notes |
|----------|-------|
| `OTLP_ENDPOINT` | Preferred Warden setting (`.env` or process env) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Fallback when `OTLP_ENDPOINT` is unset |
| `OTLP_INSECURE` | Default `true`; set `false` when the collector expects TLS |

Example for a collector reachable from the host:

```bash
OTLP_ENDPOINT=http://127.0.0.1:4317
OTLP_INSECURE=true
```

Point `OTLP_ENDPOINT` at your collector's gRPC OTLP URL (Grafana Tempo, Honeycomb, Jaeger, etc.).

### TLS and other OTLP options

Warden passes **endpoint** and **insecure** into the OTLP gRPC exporter. It does not wrap headers, certificates, or timeouts in its own settings. For those, set standard **OpenTelemetry environment variables** on the engine or worker process — **no Warden code changes required**. The SDK reads them when Warden leaves the corresponding exporter arguments unset.

| Concern | Variable(s) | Loaded by |
|---------|-------------|-----------|
| Plaintext vs TLS | `OTLP_INSECURE` (default `true`) | **Warden** — always applied; do not rely on `OTEL_EXPORTER_OTLP_INSECURE` alone |
| Collector URL | `OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` | Warden or SDK (see table above) |
| API key / auth headers | `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | OpenTelemetry SDK |
| Custom CA / mTLS | `OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE`, `…_CLIENT_KEY`, `…_CLIENT_CERTIFICATE` | OpenTelemetry SDK (when `OTLP_INSECURE=false`) |
| Timeout, compression | `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`, `…_COMPRESSION`, etc. | OpenTelemetry SDK |

For **basic TLS** with system-trusted CAs: `OTLP_INSECURE=false` and an `https://…` endpoint (or your vendor's gRPC TLS URL). An `https://` scheme also forces a secure channel per the OTel spec.

:::note[Vendor auth without TLS changes]
Many hosted collectors accept `OTEL_EXPORTER_OTLP_HEADERS` (or the `…_TRACES_HEADERS` variant) for API keys while leaving `OTLP_INSECURE=true`. Follow your vendor's OTLP gRPC docs. This pass-through is not integration-tested in the Warden repo — verify export in your collector UI.
:::

Service names in traces: `engine-node`, `worker-node`.

### Warden correlation on spans

Postgres owns workflow identity. At saga start the engine assigns `saga_instances.trace_id` (32-char hex) and each `saga_step_instances.span_id` (16-char hex) — UUID values, not copied from OpenTelemetry. CLI commands and SQL use those ids.

OpenTelemetry builds a separate **trace graph** (engine ingest, worker execution, nested operations). Each span gets OTel's own Trace ID and Span ID in the collector UI.

Warden bridges the two by setting **span attributes** on telemetry spans:

| Attribute | Value | Same as Jaeger "Trace ID" / "Span ID"? |
|-----------|-------|----------------------------------------|
| `saga.id` | Postgres `saga_instances.trace_id` | **No** — different field; filter on this attribute |
| `saga.step_span_id` | Postgres `saga_step_instances.span_id` | **No** — one step row can have many child spans |

Outbox messages also carry `trace_context` so engine and worker spans link in the trace UI. That propagation is OTel's graph; `saga.id` is still the id returned by `warden start saga`.

**To correlate:** copy `trace_id` from `warden start saga` (or `warden list sagas`), filter traces on attribute `saga.id`, then use `warden list steps --trace-id …` or query `saga_step_instances` by `saga_trace_id`. Do not assume the collector's top-level Trace ID column is your saga id.

## Execution timing

Forward steps and compensation undo rows persist structured millisecond buckets in `saga_step_instances.execution_timing` (JSONB). Timing is **never** injected into user `output_payload` / saga context `data`.

Worker result events (`STEP_COMPLETED`, `STEP_FAILED`, `STEP_COMPENSATED`, `COMPENSATION_FAILED`) carry top-level `timing.worker` on the outbox wire. The engine merges engine-side buckets at ingest.

### Bucket reference

| Bucket | Process | Forward step | Compensation undo row |
|--------|---------|--------------|------------------------|
| `hydration_ms` | Worker | Command hydrate | Same |
| `setup_ms` | Worker | Adapter + MCP bootstrap | Same |
| `llm_ms` | Worker | **`react`:** ReAct turns; **`simple`:** single structured LLM call | `0` for single-tool undo; ReAct sum for multi-tool |
| `tool_ms` | Worker | MCP invokes | Single `ainvoke` or ReAct tool sum |
| `when_cel_ms` | Engine | `when.cel` evaluation at schedule | — (FSM-driven) |
| `schedule_ms` | Engine | `trigger_step` / `trigger_compensation` | Child row create + command emit |
| `policy_ms` | Engine | Before-commit + after-reason gates | `0` today |
| `dispatch_to_ingest_ms` | Engine | End-to-end step wall-clock from worker dispatch to result ingest | Same |

**`dispatch_to_ingest_ms` is the headline step duration** — wall-clock from when the engine commits the worker command to the outbox until the engine begins ingesting the worker's result. Worker buckets (`llm_ms`, `tool_ms`, …) describe work inside that window; they overlap this value, so do not sum them for total latency.

Monotonic `time.perf_counter()` segments only; no cross-process wall-clock math between worker and engine hosts.

### Inspect timing

HTTP: `GET /v1/sagas/steps?trace_id=…` returns `timing` on each row (forward and undo via `compensates_span_id`).

```sql
-- Forward steps
SELECT span_id, step_id, status, execution_timing
FROM saga_step_instances
WHERE saga_trace_id = :trace_id
  AND compensates_span_id IS NULL;

-- Compensation undo rows
SELECT span_id, compensates_span_id, status, execution_timing
FROM saga_step_instances
WHERE saga_trace_id = :trace_id
  AND compensates_span_id IS NOT NULL;
```

See [Compensation](manifests/compensation.md) for single-tool vs ReAct undo timing expectations.

### OpenTelemetry span attributes

When `OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_ENDPOINT`) is configured, the same timing buckets are mirrored as **span attributes** on existing handler spans. Postgres `execution_timing` remains authoritative; attributes are for Jaeger/Tempo drill-down.

| Attribute prefix | Example spans |
|------------------|---------------|
| `timing.worker.*` | Worker `handle_worker_command` (`DO_STEP`, `DO_COMMIT`, `EXECUTE_COMPENSATION`) |
| `timing.engine.schedule_ms`, `policy_ms`, `when_cel_ms` | Engine `trigger_step` / `trigger_compensation` |
| `timing.engine.dispatch_to_ingest_ms` | Engine ingest handlers (`handle_step_completed`, failures, compensation ingest) |
| `timing.engine.policy_ms` (after-reason) | Engine `handle_step_completed` only |

Worker timing appears on the **worker** span, not the ingest span. Filter Jaeger on `saga.id=<trace_id>` and inspect `DO_STEP` vs `handle_step_completed` separately.

Every span that records timing attributes is also tagged with `saga.id` — including `trigger_step` (where `when_cel_ms` and `before_commit` `policy_ms` land) and `handle_step_completed` (where `after_reason` `policy_ms` and `dispatch_to_ingest_ms` land). Warden sets those attributes from the bound saga row or ingest payload, not from OpenTelemetry's wire Trace ID, so one Jaeger filter on `saga.id` surfaces scheduling, policy, and worker spans for the same run. For what those buckets mean in manifest terms, see [Conditional branching (`when.cel`)](manifests/when-cel.md) and [Policies](manifests/policies.md).

:::tip[Correlating MCP tool logs]
`saga.id` and timing attributes appear on **Warden engine and worker spans**. They are **not** automatically injected into MCP subprocess environments or external tool HTTP headers. To correlate a third-party tool failure back to a saga run, filter Jaeger on `saga.id=<trace_id>` and inspect the parent worker span, check worker logs for the command's `saga_trace_id`, or pass `trace_id` explicitly through a `with` binding into tool arguments when your integration needs it.
:::

For **`simple`** reason steps, execution time is recorded in a single `llm_ms` bucket (no tool loop). With the enterprise plugin enabled, reasoning audit captures the structured chat transcript shape (`[system, human, assistant]`) — for `simple`, the assistant message is the validated JSON string. See [Open Core vs Enterprise](../getting-started/open-core-vs-enterprise.md).

OpenTelemetry exports these attributes when the span **ends** (batch flush from the `BatchSpanProcessor`), so you will not see partial bucket updates mid-handler. Background maintenance paths — outbox reap and operator recovery — repair state only and do not emit timing attributes.

## What's next

Next up: [CLI overview](cli/overview.md) — start sagas, approve HITL holds, and retry stuck compensation from the terminal.

## Related

- [Start and monitor](cli/start-and-monitor.md) — CLI listing and monitoring
- [Saga recovery](cli/saga-recovery.md) — stuck steps and compensation retries
- [Configuration](../getting-started/configuration.md) — recovery timeouts and OTLP env vars
