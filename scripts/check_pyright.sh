#!/usr/bin/env bash
# Pyright typecheck with a grandfathered baseline (fail only on new errors).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASELINE="${ROOT}/scripts/pyright_baseline.txt"
JSON_OUT="$(mktemp)"
ERR_OUT="$(mktemp)"
trap 'rm -f "$JSON_OUT" "$ERR_OUT"' EXIT

set +e
uv run --extra dev --extra engine --extra worker --extra cli python -m pyright --outputjson >"$JSON_OUT" 2>"$ERR_OUT"
PYRIGHT_RC=$?
set -e

if [[ ! -s "$JSON_OUT" ]]; then
  echo "FAIL: pyright produced no JSON output" >&2
  if [[ -s "$ERR_OUT" ]]; then
    echo "Pyright stderr:" >&2
    cat "$ERR_OUT" >&2
  fi
  exit 1
fi

uv run python - "$JSON_OUT" "$ERR_OUT" "$BASELINE" "$ROOT" "$PYRIGHT_RC" <<'PY'
import json
import re
import sys
from collections import Counter
from pathlib import Path

json_path, err_path, baseline_path, root, pyright_rc = sys.argv[1:6]
root = Path(root)
raw_json = Path(json_path).read_text()

try:
    data = json.loads(raw_json)
except json.JSONDecodeError:
    print("FAIL: Pyright output could not be parsed as JSON. Raw output below:", file=sys.stderr)
    print(raw_json, file=sys.stderr)
    err_text = Path(err_path).read_text()
    if err_text.strip():
        print("Pyright stderr:", file=sys.stderr)
        print(err_text, file=sys.stderr)
    sys.exit(1)

if not isinstance(data, dict) or "generalDiagnostics" not in data:
    print("FAIL: Pyright JSON missing generalDiagnostics. Raw output below:", file=sys.stderr)
    print(raw_json, file=sys.stderr)
    err_text = Path(err_path).read_text()
    if err_text.strip():
        print("Pyright stderr:", file=sys.stderr)
        print(err_text, file=sys.stderr)
    sys.exit(1)

diagnostics = [
    d for d in data.get("generalDiagnostics", [])
    if d.get("severity") == "error"
]

_LEGACY_BASELINE = re.compile(r"^(.+):(\d+):(report\w+)$")


def normalize_baseline_entry(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    legacy = _LEGACY_BASELINE.match(stripped)
    if legacy:
        return f"{legacy.group(1)}:{legacy.group(3)}"
    return stripped


def fingerprint(d: dict) -> str:
    file = Path(d["file"])
    try:
        rel = file.relative_to(root)
    except ValueError:
        rel = file
    rule = d.get("rule", "unknown")
    return f"{rel}:{rule}"


current = Counter(fingerprint(d) for d in diagnostics)
baseline_file = Path(baseline_path)

if not baseline_file.exists():
    entries = sorted(current.elements())
    baseline_file.write_text(
        "# Pyright baseline fingerprints (file:rule). One line per error; duplicates allowed.\n"
        + "\n".join(entries)
        + ("\n" if entries else "")
    )
    print(f"Wrote {len(entries)} entries to {baseline_file}")
    if entries:
        print("Review scripts/pyright_baseline.txt and re-run make typecheck.")
    else:
        print("OK: pyright clean")
    sys.exit(0)

baseline = Counter(
    entry
    for line in baseline_file.read_text().splitlines()
    if (entry := normalize_baseline_entry(line)) is not None
)
new = current - baseline
removed = baseline - current

if new:
    print("FAIL: new pyright errors (not in scripts/pyright_baseline.txt):", file=sys.stderr)
    for entry, count in sorted(new.items()):
        suffix = f" (x{count})" if count > 1 else ""
        print(f"  {entry}{suffix}", file=sys.stderr)
    sys.exit(1)

if removed:
    fixed = sum(removed.values())
    print(
        f"OK: pyright passed ({fixed} baseline error(s) fixed; "
        "prune scripts/pyright_baseline.txt when convenient)"
    )
else:
    print(f"OK: pyright passed ({sum(current.values())} grandfathered errors unchanged)")

if int(pyright_rc) not in (0, 1):
    err_text = Path(err_path).read_text()
    if err_text.strip():
        print(f"WARN: pyright exited with status {pyright_rc}", file=sys.stderr)
        print(err_text, file=sys.stderr)

PY
