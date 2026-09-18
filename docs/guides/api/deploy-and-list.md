---
sidebar_position: 2
pagination_prev: guides/api/overview
pagination_next: guides/api/start-and-monitor
---

# Deploy and list

Before you can start a saga over HTTP, register worker, step, and saga definitions with the engine. `POST /v1/manifests` validates each manifest and stores the result in Postgres — runtime credentials and MCP connectivity are checked later when steps actually run.

Deploy order is **workers → steps → sagas**. Step manifests pin `(worker, worker_version)` in the saga namespace; saga `use:` refs must resolve to registered steps or deploy fails.

Each definition is tracked by `namespace`, `name`, and `version`. Leave out `namespace` in deploy YAML and the engine defaults to `"default"`.

## Deploy a manifest

```bash
curl -sS -X POST "$ENGINE_URL/v1/manifests" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/worker.github-demo.yaml
```

Dry-run (validate / link-check only — no catalog write):

```bash
curl -sS -X POST "$ENGINE_URL/v1/manifests?dry_run=true" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/saga.github-demo.yaml
```

Typical order on a fresh stack:

```bash
curl -sS -X POST "$ENGINE_URL/v1/manifests" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/worker.github-demo.yaml
# then step manifests, then the saga
curl -sS -X POST "$ENGINE_URL/v1/manifests" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/saga.github-demo.yaml
```

JSON is also accepted (`Content-Type: application/json`).

Success response (**200 OK** — synchronous; the definition is registered before the response returns, unless `dry_run=true`):

```json
{ "message": "...", "dry_run": false }
```

On validation failure the engine returns **`400`** with a `detail` string.

**Sagas** may redeploy the same `(namespace, name, version)` to update the stored authoring AST. **Steps and workers** are append-only — bump `version` to change capability. Saga deploy link-checks `use:` refs; hydration into instance `frozen_steps` happens at start.

**What deploy checks:** YAML structure, worker/step refs, artifact paths, `when` CEL, `with` vs step `inputs`, and tighten-only overrides. It does **not** validate API keys or MCP reachability. Dry-run skips persistence and does not check worker/step version immutability.

CLI equivalent: `warden deploy -f …` / `warden deploy --dry-run -f …` — see [CLI Deploy and list](../cli/deploy-and-list.md).

## List definitions

```bash
curl -sS "$ENGINE_URL/v1/definitions/sagas"
curl -sS "$ENGINE_URL/v1/definitions/steps"
curl -sS "$ENGINE_URL/v1/definitions/workers"
```

Shared optional query parameters: `namespace`, `name`, `limit`, `offset`, `include_total`, `is_active`. Soft-disable a pin with `PATCH /v1/definitions/{workers|steps|sagas}?id=<uuid>` **or** `?namespace=&name=&version=` and body `{"is_active": false}` (CLI: `warden definitions set-active -t worker --id … --inactive` or `--namespace/--name/--version`).

Deploy/start against an **inactive** catalog pin returns **409** with structured detail `code: INACTIVE_CATALOG_DEFINITION`. Missing pins return **404** with `code: CATALOG_DEFINITION_NOT_FOUND`.

The list endpoints do not filter by `version` — each item includes a `version` field. Pick the row you need client-side before `POST /v1/sagas/start`, or fetch one definition by identity (below).

Get one definition — `id` XOR `namespace+name+version` on the collection (same identity rules as PATCH), optional `include_body=true`:

```bash
curl -sS "$ENGINE_URL/v1/definitions/sagas?id=<definition-uuid>&include_body=true"
curl -sS "$ENGINE_URL/v1/definitions/steps?namespace=default&name=greet&version=0.1.0&include_body=true"
curl -sS "$ENGINE_URL/v1/definitions/workers?namespace=default&name=mock-worker&version=0.1.0"
```

Returns **`404`** with structured `CATALOG_DEFINITION_NOT_FOUND` when unknown; partial triple → **400**; invalid UUID on `id=` → **422**.

## What's next

With definitions registered, start an instance and poll until it finishes or pauses for review: [Start and monitor](start-and-monitor.md). CLI equivalent: [Deploy and list](../cli/deploy-and-list.md). Schema details: [API Reference](/docs/api/api-reference) — [manifests](/docs/api/post-manifests-v-1-manifests-post), [definitions](/docs/api/get-definitions-sagas-v-1-definitions-sagas-get).
