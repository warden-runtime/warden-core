---
title: "Demo: Quickstart"
sidebar_position: 5
sidebar_label: "Demo: Quickstart"
pagination_prev: getting-started/demo-observe-execution-timing
pagination_next: getting-started/demo-github-mcp
---

# Demo: Quickstart

You are ready to connect Warden to live inference — OpenAI in the cloud, or an OpenAI-compatible server on your network (Ollama, vLLM). Deploy a minimal worker and saga, start one instance, and confirm a real model responds. The stack from [Installation](installation.md) is the same as the mock demos.

To isolate connectivity from open-ended tool loops, this walkthrough sets **`agent-adapter: simple`**: one structured turn, no ReAct loop, no MCP tools. That gives you a clean smoke test before you add policy and HITL in [GitHub MCP](demo-github-mcp.md).

## What you'll use

| Artifact | Path |
|----------|------|
| Cloud worker | `config/worker.minimal.yaml` — `provider: openai`, `gpt-4o-mini` |
| Local worker | `config/worker.local-minimal.yaml` — `provider: local`, `llama3.2` |
| Saga | `config/saga.minimal.yaml` — step `step1`, `agent-adapter: simple`, `tools.allow: []` |
| Prompt | `config/prompts/noop.j2` |

Before you deploy, open the files in the table — especially `config/saga.minimal.yaml` and `noop.j2`. The walkthrough assumes you've seen what you're registering.

## Before you start

1. Finish [Installation](installation.md) — `warden ping` returns healthy.
2. Complete the first two demos if you have not already — [Mock LLM and MCP](demo-mock-llm-and-mcp.md) and [Observe execution timing](demo-observe-execution-timing.md).
3. Set LLM credentials in `.env` at the repo root **before** you deploy and start. The worker reads them at step runtime — `warden deploy` does not validate them.

Pick **one** worker path. Uncomment or add the matching variables in `.env`, substitute your own values, then restart the worker if the stack was already running (`docker compose up -d worker` or `make up`):

:::note[Changed `.env` after a failed run?]
LLM credentials are read by the **worker** at step runtime — not by the engine, and not at `warden deploy`. If you add or fix `OPENAI_API_KEY` (or `WARDEN_LOCAL_LLM_BASE_URL`) while Compose is already up, restart the worker so the container picks up the new values. The engine does not need a restart. A saga that already failed stays `FAILED` until you start a new instance or retry the step (`warden saga retry-step --trace-id <TRACE_ID> --step-id step1`).
:::

| Path | Worker manifest | Edit `.env` |
|------|-----------------|-------------|
| **Cloud (OpenAI)** | `config/worker.minimal.yaml` | Set `OPENAI_API_KEY=sk-...` with a valid key |
| **Local (Ollama / vLLM)** | `config/worker.local-minimal.yaml` | Run Ollama on the **host** (`ollama pull llama3.2`). Set `WARDEN_LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1` in `.env`. Under Compose you also need `OLLAMA_HOST` on the host and `extra_hosts` on the worker — see [Configuration → Local LLM under Docker (Ollama)](configuration.md#local-llm-under-docker-ollama). No API key. |

Full variable table and copy-paste examples: [Configuration → Environment variables](configuration.md#environment-variables).

## Walkthrough

From the repo root with `ENGINE_URL` set. Run commands with `source .venv/bin/activate` or prefix with `uv run` — see [Installation](installation.md).

Cloud or local LLM use the same commands; only the worker manifest from the table above changes.

```bash
warden deploy -f config/worker.minimal.yaml          # or worker.local-minimal.yaml
warden deploy -f config/saga.minimal.yaml
warden start saga -n minimal-saga -v 0.0.1 --namespace default
warden list steps --trace-id <TRACE_ID> --namespace default
```

Copy `<TRACE_ID>` from the start response (same as earlier demos). If a step is still `IN_PROGRESS`, run `list steps` again or use `warden show step` below. Add `--watch` to poll instead of re-running the command ([Start and monitor](../guides/cli/start-and-monitor.md)).

## Expected outcome

- Saga: `COMPLETED`
- Step `step1`: `COMPLETED`

The bundled prompt (`config/prompts/noop.j2`) asks the model to confirm connectivity and return a brief acknowledgment. Step `step1` has no `output_schema`, so the worker uses the built-in fallback for **`agent-adapter: simple`** (see [Try it: add an `output_schema`](#try-it-add-an-output_schema) to define your own):

```json
{"summary": "<brief acknowledgment string>"}
```

Inspect the result:

```bash
warden show step <TRACE_ID> --step-id step1 --namespace default
```

Expect `output_payload.data.summary` to be a non-empty string. If the model returns unparseable JSON, the step fails with `structured_output_failed` or `empty_structured_result` (see failure table below).

After the saga completes, compare timing to your mock demo run — use the same `show step` command; the `timing` block (or `--json` → `timing`) is what changed:

```bash
warden show step <TRACE_ID> --step-id step1 --namespace default
```

Expect `worker.llm_ms` to jump from single-digit milliseconds (mock) to **seconds** for real inference — on **`simple`** steps that bucket is a **single** LLM call, not a sum across ReAct turns ([Observability → Execution timing](../guides/observability.md#execution-timing)). With no tools allowed, `worker.tool_ms` should be absent or near zero. `engine.dispatch_to_ingest_ms` is the wall-clock envelope for the async round-trip; `worker.llm_ms` sits **inside** that envelope ([Demo: Observe Execution Timing → Wall-clock envelope](demo-observe-execution-timing.md#wall-clock-envelope)).

<details>
<summary>What happens under the hood</summary>

1. Warden creates the saga and step rows in Postgres.
2. Warden queues `DO_STEP` on `worker-commands`.
3. The worker runs a **single structured LLM turn** (`agent-adapter: simple`) against your **live** provider — no ReAct loop and no `_submit` tool.
4. The worker emits `STEP_COMPLETED` or `STEP_FAILED`; Warden advances the FSM.

</details>

## Try it: add an `output_schema`

`output_schema` is the contract between the model and saga context. On a reason step, the worker asks the LLM for JSON; Warden validates that payload against a JSON Schema file on disk and stores the result in `steps.<id>.output.data`. Downstream steps, CEL gates, and HITL read those fields — pin explicit schemas once a saga grows beyond one step.

The bundled `config/saga.minimal.yaml` omits `output_schema` on purpose. With **`agent-adapter: simple`**, the worker then uses a built-in fallback that only requires a `summary` string. You can pin that shape yourself (or tighten it) without leaving this demo:

**1. Create a schema file** at `config/schemas/minimal-connectivity.json`:

```json
{
  "type": "object",
  "required": ["summary"],
  "properties": {
    "summary": {
      "type": "string",
      "minLength": 1
    }
  },
  "additionalProperties": false
}
```

**2. Reference it on `step1`** in `config/saga.minimal.yaml` (add one line under the existing step fields):

```yaml
  - id: step1
    kind: reason
    agent-adapter: simple
    worker: minimal-worker
    worker_version: "1.0.0"
    prompt: noop.j2
    output_schema: minimal-connectivity.json
    tools:
      allow: []
```

**3. Redeploy the saga and run again:**

```bash
warden deploy -f config/saga.minimal.yaml
warden start saga -n minimal-saga -v 0.0.1 --namespace default
warden show step <TRACE_ID> --step-id step1 --namespace default
```

The repo ships unchanged — these edits are yours to make locally. If the model drifts from the schema, the step fails with a validation error in `show step` — that is Warden doing its job. Full reference: [Saga manifests → Structured output](../guides/manifests/saga-manifests.md#structured-output-output_schema).

:::tip[Feeling adventurous?]
Add fields to the schema, pass values through `with` on the step, and update `config/prompts/noop.j2` so the model knows what to return — then redeploy and run again. That is how real sagas pin structure before downstream steps, CEL, or HITL read it.
:::

## What just happened

You ran one **structured LLM call** against a live provider — no ReAct loop, no MCP tools. Warden checked your credentials at **runtime** when the worker loaded config, not at deploy. Compare `worker.llm_ms` to your mock demo: it should now be in **seconds**, not sub-millisecond. That bucket is what you tune when you add tools and governance in [GitHub MCP](demo-github-mcp.md).

## When a step shows `FAILED`

Warden checks credentials at runtime, not at deploy.

| Symptom | Likely cause |
|---------|--------------|
| `code: structured_output_failed` | Model did not return parseable JSON — common on weak local models; retry or use OpenAI for smoke tests |
| `error: empty_structured_result` | Model returned `{}` — check prompt and provider structured-output support |
| `error: no_submit_call` | Step uses default `react` adapter but model replied with text only — set `agent-adapter: simple` or steer model to `_submit` |
| `error: empty_submit_result` | `react` step: model called `_submit` with `{}` |
| Step `FAILED`; `warden list steps … --errors` shows `worker_config_load_failed (No API key found for openai…)` | Missing `OPENAI_API_KEY` in the worker container — set it in `.env`, run `docker compose up -d worker`, then start a new saga or `warden saga retry-step`. Confirm with `docker compose exec worker printenv OPENAI_API_KEY`. |
| Step `FAILED`; connection or timeout errors in `show step` | Ollama unreachable from the worker container — [Local LLM under Docker (Ollama)](configuration.md#local-llm-under-docker-ollama): `OLLAMA_HOST`, `extra_hosts`, and `WARDEN_LOCAL_LLM_BASE_URL`; restart the worker after edits |

```bash
warden list steps --trace-id <TRACE_ID> --namespace default
warden list steps --trace-id <TRACE_ID> --errors
warden show step <TRACE_ID> --step-id <STEP_ID>
```

Failed rows are marked `FAILED*` in the default table. Use `--errors` for a one-line summary (`structured_output_failed`, `no_submit_call`, etc.) without `--json`.

Worker logs: [Troubleshooting → Local stack diagnostics](troubleshooting.md#local-stack-diagnostics) (`make doctor` or `docker compose logs worker --tail=50`).

See [Troubleshooting](troubleshooting.md) for failures not covered by the table above.

## What's next

You verified live inference and compared timing to the mock path. Head to [Demo: GitHub MCP](demo-github-mcp.md) for what the first three walkthroughs omitted: CEL safety gates on commit arguments, a manual approval pause before a GitHub write, and conditional step scheduling on a live repository.

:::tip[No GitHub token or Docker socket?]
Run [Demo: GitHub MCP](demo-github-mcp.md) when you have credentials, or expand **The governed write path (read-only)** at the top of that page. Then continue to [Configuration](configuration.md).
:::

## Related

- [Saga manifests → Structured output](../guides/manifests/saga-manifests.md#structured-output-output_schema) — JSON Schema files, validation failures
- [Saga manifests → Reason step execution](../guides/manifests/saga-manifests.md#reason-step-execution-agent-adapter) — `react` vs `simple`, failure codes, YAML examples
- [Durable execution boundaries](../concepts/durable-execution.md) — reason steps and structured output
- [Configuration](configuration.md) — env reference
- [Start and monitor](../guides/cli/start-and-monitor.md) — CLI filters
- [GitHub MCP demo](demo-github-mcp.md) — policy, HITL, external MCP (`react` reason step)
