---
sidebar_position: 9
sidebar_label: Open Core vs Enterprise
pagination_prev: getting-started/troubleshooting
pagination_next: guides/manifests/overview
---

# Open Core vs Enterprise

The open kernel is a complete, transaction-bounded engine for governed agent workflows: engine, workers, CLI, outbox, CEL policy evaluation, compensation, and HITL gates. If you have been through Getting started, you have already been running it — not a demo stub, the same runtime teams deploy in production.

Enterprise is not a separate product that replaces the kernel. It is a family of **optional plugins** you turn on when you need them — the same extension model Warden uses everywhere else. Open core stays the execution loop; enterprise-maintained plugins add operational layers on top.

:::warden-accent[Enterprise plugin availability]
Enterprise-maintained plugins are not publicly available yet. [Join the waitlist](https://forms.gle/ripXfwzDQDHsYZt18) for early access.
:::

## Optional plugins, same model

Enterprise plugins work like any other Warden plugin: enable what you need, leave the rest off. The kernel keeps running sagas, the outbox, and policy gates either way. Mix and match by concern — forensic audit without RBAC, or enforcement without a ledger — or run several together. The capability areas below are **themes**, not a fixed bundle or a closed catalog.

:::tip[Not a special case]
Enterprise plugins use the same registry and install path as extensions you write yourself. For wiring details, see [Extending Warden](../advanced/extending-warden.md) and [Architecture → Plugin architecture](../advanced/architecture.md#plugin-architecture).
:::

## Capability areas (illustrative)

The examples below are **archetypes** of what enterprise-maintained plugins cover today — not an exhaustive catalog of every plugin on the roadmap.

### Governance enforcement

**What it is for:** Cluster-level SLA and expiry when inline kernel recovery is not enough — for example manifest `timeout_seconds` on steps that never report back after worker node loss, HITL review expiry, or compensation undo rows stuck past their timeout.

**Example today:** A governance control-plane process (separate from step workers) that enforces those deadlines and emits the corresponding outbox and ledger side effects.

**What the kernel already does:** Normal step execution, outbox dispatch, and automatic recovery from stale worker claims and orphaned outbox rows — see [Saga recovery → Automatic recovery](../guides/cli/saga-recovery.md#automatic-recovery). Enterprise enforcement plugins add scheduled, out-of-band reapers when that inline path is insufficient.

### Forensic evidence

**What it is for:** Tamper-evident, append-only history for regulated environments — policy outcomes, saga transitions, worker actions, HITL decisions, governance timeouts, and related operational events.

**Examples today:** A hash-chained forensic ledger in Postgres; HTTP routes such as `/v1/audit-events` and matching `warden audit` commands to list, scope, and verify chain integrity.

**What the kernel already does:** Authoritative saga and step state in Postgres, OpenTelemetry trace propagation, and policy **evaluation** (CEL outcomes). Ledger **writes** and verify APIs live in enterprise plugins — the kernel exposes hooks; it does not hardcode audit tables.

### Enterprise access

**What it is for:** Multi-team deployments where operators, reviewers, and integrators need different API and CLI permissions — especially alongside audit and HITL surfaces.

**Example today:** Identity and role-based access for engine API routes, audit endpoints, and HITL review operations.

**What the kernel already does:** Human-in-the-loop gates, operator recovery, and deploy/start APIs without built-in RBAC. Access-control plugins add authenticated, role-scoped permissions when you need them.

### Fleet scale and alternate messaging

**What it is for:** Teams that outgrow a single Postgres coordination layer — more workers across availability zones, higher fanout, or a dedicated message bus (Kafka, SQS) behind the same saga semantics.

**Design context:** Warden's messaging model was originally shaped for distributed event streams. The **open kernel ships Postgres as the supported, documented transport**. Enterprise-maintained plugins are the path to broker-backed relay and horizontal consumer groups — not a configuration toggle in open core.

**Examples on the roadmap:** Kafka or SQS adapters with outbox relay; multi-node consumer groups; automated dead-letter routing and retry policies for failed outbox rows.

**What the kernel already does:** Postgres transactional outbox, configurable worker concurrency (`WORKER_MAX_IN_FLIGHT`), automatic stale-claim and outbox reap, and operator recovery commands (`warden saga retry-*`). See [Architecture → Scaling and operational limits](../advanced/architecture.md#scaling-and-operational-limits).

## Always in the kernel

These stay in open core regardless of which enterprise plugins you enable:

- Saga FSM, transactional outbox, and worker command loop
- CEL policy evaluation and denial/HITL ordering
- Compensation (LIFO undo) and HITL pause/resume
- Operator recovery for stuck steps (`warden saga retry-*`, HTTP retry endpoints)
- Automatic claim and outbox reap (see [Configuration → Recovery timeouts](configuration.md#recovery-timeouts))

Enterprise plugins **add** observers, routes, control-plane processes, and tables — they do not remove or gate core workflow execution.

:::warden-accent[Interested in enterprise plugins?]
Enterprise-maintained plugins are not publicly available yet. [Join the waitlist](https://forms.gle/ripXfwzDQDHsYZt18) for early access and design-partnership updates.
:::

## What's next

You have seen the open kernel in action through Getting started. Continue to [Guides → Manifests and artifacts](../guides/manifests/overview.md) to author worker and saga YAML, wire MCP tools, and define the policies that bound each step.

## Related

- [Architecture](../advanced/architecture.md) — plugin registry, boot sequence, runtime topology
- [Extending Warden](../advanced/extending-warden.md) — worker ports, registry hooks, and plugin install
