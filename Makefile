## Warden local development — operator entrypoint (see `make help`).
## Implementation detail lives in docker-compose.yml; prefer these targets over raw compose commands.

COMPOSE ?= docker compose

.PHONY: help sync-dev up up-db stop down clean reset \
	build rebuild logs ps doctor migrate migrate-compose \
	run-engine \
	check check-boundary lint ruff radon typecheck audit-deps audit-semgrep audit tests upgrade docs-api

.DEFAULT_GOAL := help

help: ## Show local deploy targets (default)
	@echo "Warden — local deploy (Makefile is the operator surface; see docker-compose.yml for plumbing)"
	@echo ""
	@grep -E '^[a-zA-Z0-9_.-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Typical OSS path:  make sync-dev && make up"
	@echo "Getting started:   make up → docs/getting-started/demo-mock-llm-and-mcp.md"
	@echo "Fresh database:    make reset   (wipe volume, start full stack; migrate runs via Compose)"

# --- Dependencies ---

sync-dev: ## Install uv deps (dev + engine + worker + cli extras)
	uv sync --extra dev --extra engine --extra worker --extra cli

# --- Compose stack (migrate one-shot runs before engine/worker via depends_on) ---

up: ## Start full stack: db, migrate, engine, worker, jaeger, adminer
	@$(COMPOSE) up -d

up-db: ## Start Postgres only (then: make migrate-compose or make migrate on host)
	@$(COMPOSE) up -d postgres

stop: ## Stop containers; keep Postgres volume (engine_db_data)
	@$(COMPOSE) down --remove-orphans

down: stop ## Alias for stop

clean: ## Stop containers and delete Postgres volume (empty DB on next up)
	@$(COMPOSE) down --volumes --remove-orphans

reset: clean up ## Wipe DB volume and start full stack (migrate runs automatically)

build: ## Build engine/worker/migrate images
	@$(COMPOSE) build

rebuild: ## Build images without cache
	@$(COMPOSE) build --no-cache

ps: ## Show compose service status
	@$(COMPOSE) ps -a

logs: ## Follow logs (all services, last 50 lines)
	@$(COMPOSE) logs -f --tail=50

doctor: ## Print ps + recent migrate/engine/worker logs when something looks stuck
	@echo "=== compose ps ==="
	@$(COMPOSE) ps -a
	@echo ""
	@echo "=== migrate (last run) ==="
	@$(COMPOSE) logs migrate --tail=30 2>/dev/null || true
	@echo ""
	@echo "=== engine ==="
	@$(COMPOSE) logs engine --tail=40 2>/dev/null || true
	@echo ""
	@echo "=== worker ==="
	@$(COMPOSE) logs worker --tail=40 2>/dev/null || true
	@echo ""
	@echo "Host CLI: DB_URL must use 127.0.0.1:5432 (not postgres). Run make help."

# --- Schema ---

migrate: ## Apply migrations from host (DB_URL@127.0.0.1:5432; use when DB up, services on host)
	PYTHONPATH=. uv run python scripts/run_migrations.py

migrate-compose: ## Run migrate container once (DB must be up; alternative to make migrate)
	@$(COMPOSE) up migrate --abort-on-container-exit

upgrade: migrate ## Alias for migrate

# --- Host processes (auto-load .env; default manifest roots when unset) ---

define RUN_HOST_ENV
set -a && [ -f .env ] && . ./.env; set +a; \
export PROMPTS_ROOT="$${PROMPTS_ROOT:-./config/prompts}"; \
export POLICIES_ROOT="$${POLICIES_ROOT:-./config/policies}"; \
export SCHEMAS_ROOT="$${SCHEMAS_ROOT:-./config/schemas}";
export COMPENSATIONS_ROOT="$${COMPENSATIONS_ROOT:-./config/compensations}";
endef

run-engine: ## Run engine on host (not in Compose)
	@$(RUN_HOST_ENV) PYTHONPATH=. uv run --extra engine python -m engine.main

# --- Quality ---

LINT_PATHS = common engine workers cli.py tests
SEMGREP_PATHS = common engine workers cli.py
SEMGREP_CACHE ?= .semgrep-cache
SEMGREP_ENV = XDG_CONFIG_HOME=$(CURDIR)/$(SEMGREP_CACHE) XDG_CACHE_HOME=$(CURDIR)/$(SEMGREP_CACHE)

ruff:
	uv run ruff check $(LINT_PATHS)
	uv run ruff format --check $(LINT_PATHS)

radon:
	@./scripts/check_xenon_kernel.sh

typecheck:
	@./scripts/check_pyright.sh

lint: ruff radon typecheck

check-boundary:
	@./scripts/check_open_core_boundary.sh

check: lint check-boundary

PIP_AUDIT_CACHE ?= .pip-audit-cache

audit-deps:
	uv run pip-audit --cache-dir $(PIP_AUDIT_CACHE)

audit-semgrep:
	@mkdir -p $(SEMGREP_CACHE)
	$(SEMGREP_ENV) uv run semgrep scan --metrics off --error \
		--config p/python --config p/security-audit --config .semgrep.yml \
		$(SEMGREP_PATHS)

audit: audit-deps audit-semgrep

docs-api: ## Export OpenAPI JSON and generate docs/api reference MDX (requires website npm install)
	uv run --extra engine python scripts/export_openapi.py
	cd website && npm run gen-api-docs -- engine

tests: ## Run OSS tests with coverage (requires Docker for Postgres tests, or WARDEN_TEST_POSTGRES_URL)
	@echo "Running test suite with coverage (includes Postgres tests via Docker testcontainers)..."
	uv run --extra dev --extra engine --extra worker --extra cli python -m coverage run -m pytest tests
	uv run --extra dev python -m coverage report
