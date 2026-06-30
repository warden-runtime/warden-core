#!/usr/bin/env bash
# Enforce xenon on kernel paths; allow documented grandfathered grade-C blocks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GRANDFATHERED="${ROOT}/scripts/xenon_grandfathered_blocks.txt"
OUTPUT="$(mktemp)"
trap 'rm -f "$OUTPUT"' EXIT

set +e
uv run --extra dev python -m xenon --max-absolute B --max-modules B --max-average A common engine workers cli.py >"$OUTPUT" 2>&1
XENON_RC=$?
set -e

if [[ "$XENON_RC" -eq 0 ]]; then
  cat "$OUTPUT"
  exit 0
fi

UNEXPECTED=0
while IFS= read -r line; do
  if [[ "$line" != ERROR:xenon:* ]]; then
    echo "$line"
    continue
  fi
  block="${line#ERROR:xenon:block \"}"
  block="${block%%\"*}"
  block_name="${block##* }"
  allowed=0
  while IFS= read -r pattern || [[ -n "$pattern" ]]; do
    [[ -z "$pattern" || "$pattern" =~ ^# ]] && continue
    if [[ "$block_name" == "$pattern" ]]; then
      allowed=1
      break
    fi
  done <"$GRANDFATHERED"
  if [[ "$allowed" -eq 0 ]]; then
    echo "$line" >&2
    UNEXPECTED=1
  fi
done <"$OUTPUT"

if [[ "$UNEXPECTED" -ne 0 ]]; then
  echo "FAIL: xenon block(s) above are not grandfathered; refactor or update scripts/xenon_grandfathered_blocks.txt" >&2
  exit 1
fi

echo "OK: xenon kernel check passed (grandfathered C blocks only)"
