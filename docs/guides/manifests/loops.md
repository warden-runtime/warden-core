---
sidebar_position: 7
pagination_prev: guides/manifests/when-cel
pagination_next: guides/manifests/policies
---

# Loop blocks (`until`)

Warden sagas support a single-level **loop** block: a bounded do-while over nested `reason` / `commit` steps. After each successful body pass, the engine evaluates `until.cel`. Loops are always bounded by required `max_iterations`.

## Authoring

```yaml
steps:
  - id: refine
    kind: loop
    max_iterations: 5
    until:
      cel: "steps.validate.facts.ok == true"
    steps:
      - id: attempt
        kind: reason
        # ...
      - id: validate
        kind: commit
        # ...

  - id: finalize
    kind: commit
    # ...
```

Rules:

- `max_iterations` is required and must be `>= 1`.
- `until.cel` is required and compile-checked at deploy.
- Loop bodies may contain `reason` / `commit` only (no nested loops in v1).
- Multiple sibling loops in one saga are allowed; each has an isolated `context.loops.<id>` bucket.
- Step ids must be unique across the whole blueprint, including every loop body.
- `when.cel` inside a body is allowed; `SKIPPED` counts as a clean step completion.

## Runtime model

1. **Delay-tail materialization:** saga start creates only `[prefix] → [first loop iteration 1 body]`. Steps after the active loop are minted when `until.cel` becomes true (or when entering the next sibling loop).
2. **`forward_seq`:** every forward row gets a monotonic execution sequence. Scheduling walks `forward_seq` ASC; compensation walks `forward_seq` DESC.
3. **Latest-wins context:** `context.steps.<id>` reflects the latest iteration. History lives on `SagaStepInstance` rows (`loop_id`, `iteration`, `forward_seq`).
4. **Hard fail:** any body step failure or policy/HITL reject aborts the saga (no further iterations).
5. **Exhaustion:** if `until` stays false after `max_iterations`, the saga fails with `LOOP_EXHAUSTED` and compensates.

## Compensation

Undo resolves compensation `with` paths against the **forward row being compensated** (`output_payload` / `resolved_arguments`), not live `context.steps`, so iteration N cannot bleed into iteration 1's undo.

## CEL bindings

`until.cel` (and `when.cel`) see `input`, `steps`, `loops`, `saga`, plus `loop: { id, iteration, max_iterations }` when evaluating inside a loop.

Smoke path: `max_iterations: 1` with `until.cel: "true"` exits after one body pass and materializes the tail.
