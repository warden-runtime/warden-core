#!/usr/bin/env bash
# Concatenate docs/ folders into review bundles (*_full.md) at repo root.
# Order follows website/sidebars.ts where applicable.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

declare -A ORDER
ORDER[concepts]="docs/concepts/terminology.md docs/concepts/durable-execution.md docs/concepts/lifecycle.md"
ORDER[getting-started]="docs/getting-started/prerequisites.md docs/getting-started/installation.md docs/getting-started/demo-mock-llm-and-mcp.md docs/getting-started/demo-observe-execution-timing.md docs/getting-started/demo-quickstart.md docs/getting-started/demo-github-mcp.md docs/getting-started/configuration.md docs/getting-started/troubleshooting.md docs/getting-started/open-core-vs-enterprise.md"
ORDER[guides]="docs/guides/manifests/overview.md docs/guides/manifests/worker-manifests.md docs/guides/manifests/saga-manifests.md docs/guides/manifests/prompts.md docs/guides/manifests/mcp-and-tools.md docs/guides/manifests/when-cel.md docs/guides/manifests/policies.md docs/guides/manifests/compensation.md docs/guides/cli/overview.md docs/guides/cli/deploy-and-list.md docs/guides/cli/start-and-monitor.md docs/guides/cli/hitl-review.md docs/guides/cli/saga-recovery.md docs/guides/api/overview.md docs/guides/api/deploy-and-list.md docs/guides/api/start-and-monitor.md docs/guides/api/hitl.md docs/guides/api/recovery.md docs/guides/observability.md"
ORDER[api]="docs/guides/api/overview.md docs/guides/api/deploy-and-list.md docs/guides/api/start-and-monitor.md docs/guides/api/hitl.md docs/guides/api/recovery.md"
ORDER[advanced]="docs/advanced/testing.md docs/advanced/architecture.md docs/advanced/extending-warden.md docs/advanced/migrations-and-schema.md"
ORDER[dev]="docs/dev/admonition-preview.md docs/dev/intro-writing-brief.md docs/dev/_temp.md"
ORDER[docs_root]="docs/introduction.md"

concat_files() {
  local title="$1"
  local subtitle="$2"
  local out="$3"
  shift 3
  local -a files=("$@")
  local count=0

  {
    echo "# ${title}"
    echo ""
    echo "> ${subtitle}"
    echo ""
    for f in "${files[@]}"; do
      if [[ ! -f "$f" ]]; then
        echo "warning: missing ${f}" >&2
        continue
      fi
      count=$((count + 1))
      echo "---"
      echo ""
      echo "<!-- SOURCE: ${f} -->"
      echo ""
      cat "$f"
      echo ""
      echo ""
    done
  } >"$out"
  echo "wrote ${out} ($(wc -l <"$out") lines, ${count} files)"
}

read -r -a concept_files <<<"${ORDER[concepts]}"
read -r -a gs_files <<<"${ORDER[getting-started]}"
read -r -a guide_files <<<"${ORDER[guides]}"
read -r -a api_files <<<"${ORDER[api]}"
read -r -a advanced_files <<<"${ORDER[advanced]}"
read -r -a dev_files <<<"${ORDER[dev]}"
read -r -a root_files <<<"${ORDER[docs_root]}"

concat_files \
  "concepts (concatenated documentation)" \
  "Auto-generated bundle of \`docs/concepts/\` (sidebar order)." \
  "${REPO_ROOT}/concepts_full.md" \
  "${concept_files[@]}"

concat_files \
  "getting-started (concatenated documentation)" \
  "Auto-generated bundle of \`docs/getting-started/\` (sidebar order)." \
  "${REPO_ROOT}/getting-started_full.md" \
  "${gs_files[@]}"

concat_files \
  "guides (concatenated documentation)" \
  "Auto-generated bundle of \`docs/guides/**\` (sidebar order)." \
  "${REPO_ROOT}/guides_full.md" \
  "${guide_files[@]}"

concat_files \
  "API (concatenated documentation)" \
  "Workflow guides (\`docs/guides/api/\`) plus OpenAPI route index and schema. Generated MDX on the site lives under **API Reference**." \
  "${REPO_ROOT}/api_full.md" \
  "${api_files[@]}"

{
  echo "---"
  echo ""
  echo "# API Reference — route index"
  echo ""
  echo "Auto-generated from \`website/openapi/engine.openapi.json\`. Regenerate with \`make docs-api\`."
  echo ""
  echo "| Method | Path | Tag | Summary |"
  echo "|--------|------|-----|---------|"
  uv run --extra engine python - <<'PY'
import json
from pathlib import Path

spec = json.loads(Path("website/openapi/engine.openapi.json").read_text(encoding="utf-8"))
for path in sorted(spec.get("paths", {})):
    for method, op in spec["paths"][path].items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        tags = ", ".join(op.get("tags") or [])
        summary = (op.get("summary") or op.get("description") or "").replace("\n", " ").replace("|", "\\|")
        print(f"| {method.upper()} | `{path}` | {tags} | {summary} |")
PY
  echo ""
  echo "---"
  echo ""
  echo "# Reference — OpenAPI schema (JSON)"
  echo ""
  echo '```json'
  cat "${REPO_ROOT}/website/openapi/engine.openapi.json"
  echo '```'
  echo ""
} >>"${REPO_ROOT}/api_full.md"
echo "appended OpenAPI reference to ${REPO_ROOT}/api_full.md ($(wc -l <"${REPO_ROOT}/api_full.md") lines total)"

concat_files \
  "advanced (concatenated documentation)" \
  "Auto-generated bundle of \`docs/advanced/**\` (sidebar order)." \
  "${REPO_ROOT}/advanced_full.md" \
  "${advanced_files[@]}"

concat_files \
  "dev (concatenated documentation)" \
  "Auto-generated bundle of \`docs/dev/\` markdown sources. Do not edit for publishing—edit the source files under \`docs/\`." \
  "${REPO_ROOT}/dev_full.md" \
  "${dev_files[@]}"

concat_files \
  "docs root (concatenated documentation)" \
  "Auto-generated bundle of top-level \`docs/*.md\` files (excludes \`docs-todo.md\` — internal tracker only)." \
  "${REPO_ROOT}/docs_root_full.md" \
  "${root_files[@]}"

warden_files=(
  docs/introduction.md
  "${concept_files[@]}"
  "${gs_files[@]}"
  "${guide_files[@]}"
)
{
  echo "# Warden Core — documentation review bundle"
  echo ""
  echo "Single-file export of **Intro**, **Core concepts**, **Getting started**, and **Guides** (per \`website/sidebars.ts\`). Advanced and dev docs are not included."
  echo ""
  echo "Generated from \`docs/\` in the warden-core repository. Internal links use Docusaurus paths (e.g. \`/docs/guides/...\`) and may not resolve in plain Markdown viewers."
  echo ""
  count=0
  for f in "${warden_files[@]}"; do
    if [[ ! -f "$f" ]]; then
      echo "warning: missing ${f}" >&2
      continue
    fi
    count=$((count + 1))
    echo "---"
    echo ""
    echo "<!-- source: ${f} -->"
    echo ""
    cat "$f"
    echo ""
    echo ""
  done
} >"${REPO_ROOT}/warden-core-full.md"
echo "wrote ${REPO_ROOT}/warden-core-full.md ($(wc -l <"${REPO_ROOT}/warden-core-full.md") lines, ${count} files)"

ls -la "${REPO_ROOT}"/*_full.md "${REPO_ROOT}/warden-core-full.md"
