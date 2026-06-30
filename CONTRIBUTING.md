# Contributing to Warden

Thank you for helping improve Warden. This repository is the **open-core kernel** — saga engine, workers, CLI, manifests, and docs.

## Before you start

1. Read the **[Introduction](docs/introduction.md)** for the mental model (sagas, outbox, policy gates, compensation).
2. Skim **[Open Core vs Enterprise](docs/getting-started/open-core-vs-enterprise.md)** so you know what belongs in the kernel versus optional plugins.
3. For architecture and extension points, see **[Architecture](docs/advanced/architecture.md)** and **[Extending Warden](docs/advanced/extending-warden.md)**.

Questions and design discussion: open a GitHub issue, or email **[authors@warden-runtime.org](mailto:authors@warden-runtime.org)**. Bug reports and feature requests are welcome.

## Development setup

```bash
cp .env.example .env   # adjust if needed
make sync-dev          # uv deps (dev + engine + worker + cli)
make up                # Postgres, migrate, engine, worker (Compose)
```

Credential-free walkthrough after `make up`:

```bash
# See docs/getting-started/demo-mock-llm-and-mcp.md
warden deploy -f config/worker.mock-mcp.yaml
warden deploy -f config/saga.mock-mcp.yaml
warden start saga -n mock-mcp-saga -v 0.1.0 --input '{"name":"Ada"}'
```

Docs site locally:

```bash
make docs-api          # after API changes
cd website && npm install && npm start
```

Full testing and lint detail: **[Testing](docs/advanced/testing.md)**.

## Pull request checklist

Before opening a PR, run:

```bash
make check    # ruff, xenon, typecheck, open-core import boundary
make tests    # pytest with coverage (Docker for Postgres slice)
```

For doc-only changes that touch the site:

```bash
cd website && npm run build
```

### Code guidelines

- **Python 3.11+**, async-first, Pydantic v2 at boundaries, absolute imports.
- Match existing style in the file you edit; run `uv run ruff check --fix` and `uv run ruff format` on touched paths.
- New or heavily edited kernel functions must stay at **xenon grade B** or better (`make check` enforces this on `common/`, `engine/`, `workers/`, `cli.py`).
- Add or update tests for behavior changes. Prefer `tests/unit/` for isolated logic; use `tests/integration/` or `tests/postgres/` when SQL, outbox, or locking matter.

### Open-core boundary (required for kernel changes)

The kernel (`common/`, `engine/`, `workers/`, `cli.py`) **must not import** `enterprise` or legacy audit modules. Extend behavior through **`common/plugins/` registry hooks** with NoOp defaults.

`make check-boundary` runs `scripts/check_open_core_boundary.sh` and fails on violations.

| Change type | Where it goes |
|-------------|---------------|
| Saga FSM, outbox, policy evaluation | Kernel |
| Forensic ledger, audit HTTP/CLI, governance reapers | Private `warden-enterprise` plugin via `WARDEN_PLUGINS` |
| New optional side effect | Protocol + NoOp in `common/plugins/`, call site in kernel, observer in plugin |

OSS tests use the default NoOp registry. Enterprise plugin tests live in the separate **warden-enterprise** repository.

### Documentation

- User-facing manual: `docs/` (published via Docusaurus).
- Keep the root **README** a short gateway; put depth in the manual.
- Use relative `.md` links between doc pages. Run `npm run build` in `website/` before merging doc refactors (`onBrokenLinks: 'throw'`).

## Commit messages

Write clear, imperative subject lines (`Fix outbox reap race`, `Document recovery idempotency`). Body optional; explain *why* when the diff is not obvious.

## Licensing

By contributing, you agree that your contributions are licensed under the **[Apache License 2.0](LICENSE)**. The project copyright holder is **The Warden Authors**.

Published documentation: **[warden-runtime.org](https://warden-runtime.org)** (in-repo sources under `docs/`).

## Enterprise plugin and waitlist

**Enterprise-maintained plugins are not shipped in this repository** — see the waitlist on [Open Core vs Enterprise](docs/getting-started/open-core-vs-enterprise.md). Contributions that only make sense with unreleased enterprise SKUs may be deferred; kernel-first changes are always preferred.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Report conduct concerns to **[conduct@warden-runtime.org](mailto:conduct@warden-runtime.org)**, or open an issue with a `[conduct]` prefix and request a private follow-up in the body.
