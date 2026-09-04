---
sidebar_position: 8
pagination_prev: guides/manifests/loops
pagination_next: guides/manifests/policies
---

# Child sagas (`spawn_sagas` / `join_sagas`)

Warden can fan out work to **child saga instances** and wait for them with an engine-native join barrier. Children stay ordinary linear sagas (reason/commit/loops). The parent does not run parallel forward steps inside one FSM.

v1 supports **wait_all** only. There is no cooperative `CANCEL`, no join timeout auto-abort, and no `wait_any` / quorum.

## Authoring

```yaml
steps:
  - id: plan
    use: plan-work
    version: "1.0.0"
    with: {}
    # catalog reason step produces output.items: [{ "id": "a", ... }, ...]

  - id: dispatch
    kind: spawn_sagas
    spawn:
      saga_name: "child-work"
      saga_version: "1.0.0"
      items_from: "$.steps.plan.output.data.items"
      item_var: "item"
      result_from: "$.steps.finalize.output.data"   # REQUIRED
      max_children: 8                              # optional; engine hard cap is 16
      input:
        payload:
          from: "$.item"
        shared_flag:
          from: "$.input.shared_flag"

  - id: await_children
    kind: join_sagas
    join:
      spawn_step_id: "dispatch"
      allow_zero_success: true                     # default true

  - id: reduce
    use: reduce-children
    version: "1.0.0"
    with: {}
    # catalog reason step reads $.steps.await_children.output.data.children
```

Rules:

- Each item in `items_from` must be an object with a non-empty string `id` (used for idempotent child starts).
- Empty `items_from` fails spawn with `SPAWN_EMPTY_ITEMS`.
- More than `max_children` (default/hard max **16**) fails with `TOO_MANY_CHILDREN`.
- `result_from` is required and must be a JSONPath into each **child** saga context.
- Resolve context for `spawn.input` is the parent context plus `$.item` and `$.{item_var}`.
- Children inherit the parent **namespace**. Deploy the child saga definition before the parent; the child must be **active** at parent deploy time.
- Spawning an inactive or missing child definition fails the spawn step (`SPAWN_CHILD_DEFINITION_INACTIVE` / `SPAWN_CHILD_DEFINITION_NOT_FOUND`) instead of raising an unhandled exception.
- Child start hydrate / asset freeze failures (missing schema, inactive step, invalid embed) fail the spawn step with `SPAWN_CHILD_HYDRATE_FAILED` and abort remaining child creations.
- Spawn/join are **not** allowed inside loop bodies.
- Each `join.spawn_step_id` must reference a `spawn_sagas` step; at most one join per spawn.

## Runtime model

1. **`spawn_sagas`** runs in the engine (no worker command). It starts one child saga per item with `start_idempotency_key = sha256(parent_trace:spawn_step_id:item_id)` and writes `saga_children` link rows. `parent_trace_id` is set on each child instance.
2. **`join_sagas`** parks `IN_PROGRESS` until every linked child reaches a terminal status (`COMPLETED`, `FAILED`, or `COMPENSATED`).
3. On child terminalization, the engine wakes the parent join (same transaction family as saga completion/failure/compensation).
4. Join output is written to `steps.<join_id>.output.data`:

```json
{
  "summary": { "total": 2, "succeeded": 1, "failed": 1 },
  "children": [
    {
      "item_id": "a",
      "child_trace_id": "...",
      "status": "COMPLETED",
      "output": { "...": "..." },
      "error": null
    },
    {
      "item_id": "b",
      "child_trace_id": "...",
      "status": "COMPENSATED",
      "output": null,
      "error": { "code": "...", "message": "..." }
    }
  ]
}
```

- `summary.succeeded` counts `COMPLETED`.
- `summary.failed` counts `FAILED` + `COMPENSATED`.
- Child row `status` preserves the raw terminal status.
- On `COMPLETED`, `output` is `result_from` (or `null` if missing). On failure terminals, `output` is `null` and `error` is populated.
- If `allow_zero_success: false` and no child completed, the join step fails with `ALL_CHILDREN_FAILED`.

## Isolation note

Hermeticity for mutating work (sandboxes, installs, tests) comes from **each child saga’s own environment**, not from extra Warden worker processes. Prefer one sandbox (or equivalent) per child for side-effecting arms.

## Compensation

When the parent compensates:

- `join_sagas` is skipped (no side effects).
- `spawn_sagas` **blocks** until all of its children are terminal, then continues LIFO. v1 does **not** cancel in-flight children; it waits for them to finish on their own.

## Recovery

Parked `join_sagas` steps have no `worker-commands` row, so outbox claim reap does not treat them as stuck workers.

Operator `warden saga retry-step` on:

- **`join_sagas`** — calls `try_complete_join` (missed-wake recovery); never dispatches `DO_STEP`.
- **`spawn_sagas`** — re-enters the idempotent spawn path.

Future step-timeout reapers must exclude these kinds (or treat join as a join wake, never worker timeout).

## Observability

List children of a parent:

```bash
warden list sagas --parent-trace-id <parent_trace_id>
# GET /v1/sagas?parent_trace_id=...
```

Saga instance JSON includes `parent_trace_id` when set.
