---
sidebar_position: 6
sidebar_label: Migrations & schema
pagination_prev: advanced/extending-warden
---

# Migrations and schema

Warden does not create database tables at process startup. Schema comes from ordered SQL in `db/migrations/`; you apply it once (or after an upgrade), then engine and worker verify the database is ready before they serve traffic. On a normal first run, `make up` handles that for you.

To protect data integrity, Warden enforces a **fail-fast** startup policy. If engine or worker processes detect that migrations have not run or are only partially applied, they halt immediately with an explicit log message rather than running against an inconsistent schema. The sections below map the tables, how migration tracking works, how to apply SQL in different setups, and how to extend the schema safely.

:::note[Reference page]
For Compose layouts and Makefile targets see [Installation](../getting-started/installation.md). For how schema fits the runtime topology see [Architecture](architecture.md).
:::

## What lives in Postgres

SQL under `db/migrations/` is the **source of truth** for kernel tables. Tortoise models in `common/models.py` mirror those tables for application code — they do **not** generate or alter schema at runtime.

| Group | Tables | Role |
|-------|--------|------|
| **Definitions** | `saga_definitions`, `worker_definitions`, `provider_secrets` | Deployed manifests and provider credentials |
| **Runtime** | `saga_instances`, `saga_step_instances` | Running sagas and per-step state |
| **Messaging** | `outbox_events`, `processed_commands`, `processed_ingest_events`, `processed_operator_recoveries` | Transactional outbox and idempotency guards |

After `make up`, inspect the live database with `\dt` in psql or Adminer at `http://127.0.0.1:8080`.

## How migrations are tracked

The migration runner is `scripts/run_migrations.py` (invoked by `make migrate` and the Compose **migrate** service). It works like this:

1. **Order** — Files in `db/migrations/` apply in **filename sort order** (`000_initial_schema.sql`, then any additive `001_…` files you add later).
2. **Ledger** — After each file succeeds, a row is inserted into **`schema_migrations`** (`version` = file stem, `applied_at` timestamp).
3. **Pending detection** — On each run, the script compares disk files to `schema_migrations` and applies only files whose stem is not yet recorded.
4. **Dry run** — List pending files without applying: `uv run python scripts/run_migrations.py --dry-run`.

At startup, engine and worker call `assert_core_schema_ready()` in `common/db_startup.py` — a lightweight check for required tables and a small set of sentinel columns. That validates the database is usable; it does **not** replace the migration ledger. If migrate was skipped, you still see the errors in [Handling initialization failures](#handling-initialization-failures).

At launch, **`000_initial_schema.sql`** is the full OSS baseline (saga/outbox/kernel tables, recovery columns, operator-recovery idempotency). Add numbered files for post-launch kernel changes.

## Extending the database schema

Warden decouples runtime ORM models from DDL. If you add kernel tables or columns, update **both** the Python model and a new SQL migration — changing `common/models.py` alone will not alter Postgres.

**Steps for kernel changes:**

1. **Update the Tortoise model** — Add fields or models in `common/models.py` so engine/worker code can read and write them.
2. **Add a numbered SQL file** — Append the next sequential migration under `db/migrations/` (for example `001_add_my_feature.sql`):

```sql
-- db/migrations/001_add_my_feature.sql
ALTER TABLE saga_definitions ADD COLUMN plugin_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
```

3. **Apply locally** — `make migrate` (or `make up` on a fresh volume).
4. **Extend startup checks if needed** — If new columns are required before serve, add them to `REQUIRED_CORE_COLUMNS` in `common/db_startup.py` so partial migrate states fail fast.

:::warning[Model without migration]
At boot, Tortoise expects columns declared on models to exist in Postgres. If you update Python models but skip the SQL file, services fail to start or queries error at runtime. Always ship model + migration together.
:::

**Plugin and enterprise tables** — Packages loaded via `WARDEN_PLUGINS` may register extra Tortoise model modules. Those tables must exist in the same migration chain **before** processes that use them start. Enterprise tables and SQL ship from the private **warden-enterprise** repository (`enterprise/models.py`, `db/migrations/`). See [Extending Warden](extending-warden.md) and [Architecture → Plugin architecture](architecture.md#plugin-architecture).

## Apply migrations

**First run or fresh clone** — migrations run automatically:

```bash
make up
```

Compose starts Postgres, runs the one-shot **migrate** service (`scripts/run_migrations.py`), then brings up engine and worker. You do not run migrate by hand for a normal boot.

| Situation | What to run |
|-----------|-------------|
| Wipe dev data and start clean | `make reset` (same as `make clean` then `make up`) |
| Postgres only — services on the host | `make up-db`, then `make migrate-compose` or `make migrate` with host `DB_URL` (`127.0.0.1:5432`) |
| Database already up — apply SQL from the host | `make migrate` |
| See pending files without applying | `uv run python scripts/run_migrations.py --dry-run` |

## Running production migrations

Core SQL in `db/migrations/` is designed to be **additive** where possible — new tables, nullable columns, `ADD COLUMN … DEFAULT` — so you can often apply migrations while older engine/worker binaries still run. Follow this order in production:

1. Apply pending SQL (`make migrate` or your pipeline equivalent).
2. Roll out engine/worker versions that depend on new columns or tables.

:::info[Production migration policy]
- Prefer **backward-compatible** DDL: new nullable columns, new tables, new indexes — avoid in-place renames of live columns under running code.
- **Heavy DDL** on hot tables (`saga_step_instances`, `outbox_events`) can block writers. Schedule disruptive changes in a maintenance window — the kernel does not drain in-flight sagas automatically.

Warden relies on **external traffic gating** rather than an internal schema-lock command. To migrate safely at scale, route active traffic away from engine nodes or use a blue-green cutover before applying schema updates, then deploy binaries that depend on the new schema.
:::

## Handling initialization failures

If an engine or worker node starts before its database has finished migrating, initialization stops and logs one of these validation errors:

```text
Database schema is not initialized (missing tables: …)
```

The target database is empty, migrate never ran, or the initial schema file failed to apply. Run `make migrate` or confirm the Compose **migrate** service completed.

```text
Database schema is out of date (missing columns: …)
```

Application code expects columns that are not present yet — usually code newer than the applied migrations. Run `make migrate` before starting services, or apply pending files in your deploy pipeline before rolling out new container images.

**Local recovery:**

```bash
make doctor          # health check — includes migrate status hints
make reset           # wipe dev volume and re-apply all migrations via make up
docker compose logs migrate   # inspect one-shot migrate container
```

More context: [Configuration → Dev stack](../getting-started/configuration.md#dev-stack-makefile-and-ports) and [Troubleshooting](../getting-started/troubleshooting.md#local-stack-diagnostics).

## What's next

Schema is the foundation everything else assumes — manifests, sagas, and outbox rows all land in these tables. When you extend Warden with plugin models or kernel changes, add SQL to `db/migrations/` and extend startup checks when new columns are required. Recovery columns in `000_initial_schema.sql` are exercised in [Testing → PostgreSQL tests](testing.md#postgresql-tests) and documented for operators in [Saga recovery](../guides/cli/saga-recovery.md).

## Related

- [Installation](../getting-started/installation.md)
- [Configuration](../getting-started/configuration.md)
- [Architecture](architecture.md)
- [Testing](testing.md)
