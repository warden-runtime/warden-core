---
sidebar_position: 2
pagination_prev: guides/api/overview
pagination_next: guides/api/start-and-monitor
---

# Deploy and list

Before you can start a saga over HTTP, register worker and saga definitions with the engine. `POST /v1/manifests` validates each manifest and stores the result in Postgres — runtime credentials and MCP connectivity are checked later when steps actually run.

Deploy the worker manifest first — saga steps reference `(worker, worker_version)` in the saga's namespace, and the engine rejects a saga deploy if that worker row is missing.

Each definition is tracked by `namespace`, `name`, and `version`. Leave out `namespace` in deploy YAML and the engine defaults to `"default"`.

## Deploy a manifest

```bash
curl -sS -X POST "$ENGINE_URL/v1/manifests" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/worker.minimal.yaml
```

Typical order on a fresh stack:

```bash
curl -sS -X POST "$ENGINE_URL/v1/manifests" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/worker.minimal.yaml
curl -sS -X POST "$ENGINE_URL/v1/manifests" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/saga.minimal.yaml
```

JSON is also accepted (`Content-Type: application/json`).

Success response (**200 OK** — synchronous; the definition is registered before the response returns):

```json
{ "message": "..." }
```

On validation failure the engine returns **`400`** with a `detail` string.

Redeploying the same `(namespace, name, version)` updates the stored definition — deploy is idempotent.

**What deploy checks:**

- YAML structure and required fields
- Worker references in saga steps
- Prompt, policy, `output_schema`, and compensation file paths (policy CEL is compile-checked)
- CEL expression syntax in `when` conditions

It does **not** validate API keys or MCP server reachability — those surface at step execution time. A policy removed from disk after deploy still fails at gate time (`errored`).

CLI equivalent:

```bash
warden deploy -f config/worker.minimal.yaml
warden deploy -f config/saga.minimal.yaml
```

## List saga definitions

```bash
curl -sS "$ENGINE_URL/v1/definitions/sagas"
```

Optional query parameters:

| Parameter | Description |
|-----------|-------------|
| `namespace` | Filter by namespace |
| `name` | Exact definition name |
| `is_active` | `true` / `false` |
| `limit` | Page size (default 50, max 100) |
| `offset` | Pagination offset |

The list endpoint does not filter by `version` — each item includes a `version` field, so pick the row you need client-side (often combine `namespace` + `name`) before calling `POST /v1/sagas/start` with an explicit `version`.

Response shape:

```json
{
  "items": [
    {
      "id": "...",
      "namespace": "default",
      "name": "minimal-saga",
      "version": "0.0.1",
      "is_active": true,
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "limit": 50,
  "offset": 0
}
```

## List worker definitions

```bash
curl -sS "$ENGINE_URL/v1/definitions/workers"
```

Same pagination and `namespace` / `name` filters as sagas (no `is_active` on workers).

## Get one saga definition by id

```bash
curl -sS "$ENGINE_URL/v1/definitions/sagas/<definition-uuid>"
```

Returns **`404`** when the id is unknown.

## What's next

With definitions registered, start an instance and poll until it finishes or pauses for review: [Start and monitor](start-and-monitor.md). CLI equivalent: [Deploy and list](../cli/deploy-and-list.md). Schema details: [API Reference](/docs/api/api-reference) — [manifests](/docs/api/post-manifests-v-1-manifests-post), [definitions](/docs/api/get-definitions-sagas-v-1-definitions-sagas-get).
