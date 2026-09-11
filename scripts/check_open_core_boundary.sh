#!/usr/bin/env bash
# Enforce kernel import gate: core paths must not import optional plugin packages or legacy audit modules.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KERNEL_PATHS=(common engine workers cli.py)
FAIL=0

check_grep() {
  local label="$1"
  local pattern="$2"
  shift 2
  if git grep -nE "$pattern" -- "$@" 2>/dev/null; then
    echo "FAIL: $label matched in kernel paths" >&2
    FAIL=1
  fi
}

check_grep "enterprise imports in kernel" \
  'from enterprise|import enterprise' \
  "${KERNEL_PATHS[@]}"

check_grep "legacy common.audit imports in kernel" \
  'common\.audit|common\.audit_causation|common\.schemas\.audit' \
  "${KERNEL_PATHS[@]}"

check_grep "audit routes/CLI in kernel" \
  'audit_app|/v1/audit-events' \
  cli.py "${KERNEL_PATHS[@]}"

# warden-mcp is an HTTP client of the engine — never import kernel packages.
if git grep -nE \
  '^[[:space:]]*(from (common|engine|workers|cli)(\.|[[:space:]])|import (common|engine|workers|cli)(\.|[[:space:]]|$))' \
  -- mcp/src mcp/tests 2>/dev/null; then
  echo "FAIL: warden_mcp imported kernel packages (common/engine/workers/cli)" >&2
  FAIL=1
fi

if [[ "$FAIL" -ne 0 ]]; then
  exit 1
fi

echo "OK: kernel import gate clean (${KERNEL_PATHS[*]}); warden-mcp has no kernel imports"
