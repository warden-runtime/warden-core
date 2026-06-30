---
title: "Demo: GitHub MCP"
sidebar_position: 6
sidebar_label: "Demo: GitHub MCP"
pagination_prev: getting-started/demo-quickstart
pagination_next: getting-started/configuration
---

# Demo: GitHub MCP

Hand an LLM a GitHub token and it can triage your issues and post comments in one pass. The hard part is trusting that write — wrong issue, text you would not ship, or a request that fires before anyone looks.

Scripts break in the middle and leave no record of where. They make writes no one reviewed. They can't pause for a human without polling logic you have to build yourself. Warden handles all of that without you writing any of it.

In this demo you run a governed saga against your repo through the [official GitHub MCP Server](https://github.com/github/github-mcp-server). You triage with read-only tools first, block bad comment payloads before any write leaves the engine, approve the post yourself, and keep every step in Postgres when you need to inspect or recover.

:::tip[No GitHub token or Docker socket access?]
This demo needs a GitHub PAT, LLM credentials, and the dev stack's Docker socket mount for stdio MCP. Skip the [Runbook](#runbook) if you cannot run it on this machine — expand below for how Warden handles the write path without executing commands.

<details>
<summary><b>The governed write path (read-only)</b></summary>

Even without running the stack, the lifecycle of a high-risk write shows how Warden enforces boundaries:

1. **Deterministic branching** — Warden extracts facts (like `total_count`) from raw MCP JSON, not from the LLM. When the count is zero, `when.cel` skips **`post-comment`** entirely.
2. **Pre-flight guardrails** — When a commit step schedules, Warden evaluates your `before_commit` CEL policy in the engine before the worker calls GitHub. Bad arguments fail the step with no API write.
3. **Human gate (HITL)** — When policy passes, Warden pauses at **AWAITING_HUMAN**. The write stays staged in Postgres until you run `warden review approve` or `reject`.

Deeper reference: [Conditional branching (`when.cel`)](../guides/manifests/when-cel.md), [Policies](../guides/manifests/policies.md), [HITL review](../guides/cli/hitl-review.md).

</details>
:::

## What runs where

| Component | Where | Details |
|-----------|-------|---------|
| Postgres + engine + worker | `make up` | Full OSS stack in Compose |
| GitHub MCP | Ephemeral container per tool session | Worker runs `docker run -i …` via mounted `/var/run/docker.sock` |
| LLM | OpenAI (default) or local Ollama | Reason step **`react`** loop (default `agent-adapter`; triage uses tools + `_submit`) |

## Prerequisites

Work through the prior demos in order — [Mock LLM and MCP](demo-mock-llm-and-mcp.md) → [Observe execution timing](demo-observe-execution-timing.md) → [Quickstart](demo-quickstart.md). You'll have a much easier time here once deploy, timing, and live inference are familiar. Then:

- Pull the MCP server image: `docker pull ghcr.io/github/github-mcp-server`. Docker must be running on the host.
- Add a [GitHub PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) to `.env` as `GITHUB_PERSONAL_ACCESS_TOKEN` with at least `repo` scope (read + issue comments on your target repo).
- Set your LLM credentials: `OPENAI_API_KEY` for `provider: openai`, or `WARDEN_LOCAL_LLM_BASE_URL` for `provider: local`. Local models need no API key.
- Sync dependencies: `make sync-dev`.

:::note[Concepts in this demo]
You will see YAML for `facts`, `when.cel`, CEL policy, and HITL in this demo. For a thorough explanation of each, see the Guides:

- [Conditional branching (`when.cel`)](../guides/manifests/when-cel.md) — schedule-time gates, CEL bindings
- [Saga manifests](../guides/manifests/saga-manifests.md) — step kinds, [tool facts](../guides/manifests/saga-manifests.md#tool-facts-facts)
- [Policies](../guides/manifests/policies.md) — CEL `before_commit` / `after_reason`
- [HITL review](../guides/cli/hitl-review.md) — approve, reject, retry
- [Worker manifests](../guides/manifests/worker-manifests.md) + [MCP and tools](../guides/manifests/mcp-and-tools.md) — Docker stdio MCP
:::

## Environment

Copy `.env.example` to `.env`. Postgres defaults are already in the template — change all four Postgres variables together if you use your own database name, user, or password. Then set:

```bash
ENGINE_URL=http://127.0.0.1:8000

# GitHub MCP — forwarded into the docker run subprocess by the worker
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...

# OpenAI (omit when using provider: local or provider: mock)
OPENAI_API_KEY=sk-...

# Ollama instead of OpenAI — also set provider: local in config/worker.github-demo.yaml
# WARDEN_LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
# Host Ollama + Docker worker: see Configuration → Local LLM under Docker (Ollama)
```

Leave `PROMPTS_ROOT` unset in `.env` when running under Compose — see [Configuration → Disk artifact roots](configuration.md#disk-artifact-roots). Full variable table: [Configuration](configuration.md).

If the stack was already running when you edited `.env`, restart the worker (`docker compose up -d worker`) before deploy or start — see [Configuration → Environment variables](configuration.md#environment-variables).

To use Ollama, edit `config/worker.github-demo.yaml`:

```yaml
provider: local
model_name: llama3.2
```

## What you'll use

Deploy two manifests to Postgres; Warden reads prompts, policies, and schemas from disk at step time. Open the files in the table before you deploy — especially the saga and policy YAML.

| Layer | Artifact | Role |
|-------|----------|------|
| Manifests | `config/worker.github-demo.yaml` | Worker + GitHub MCP (`tool_sources`) |
| | `config/saga.github-demo.yaml` | Steps `triage` (reason) → `post-comment` (commit) |
| Guardrails | `config/prompts/github-triage.j2` | Prompt for the triage reason step |
| | `config/policies/github-issue-comment.yaml` | `before_commit` guardrails on the comment payload |
| Contracts | `config/schemas/github-triage-output.json` | Required shape for triage `_submit` output |

On a run, **`triage`** loads the prompt, calls read-only MCP tools, and validates `_submit` against the schema. **`post-comment`** schedules when `when.cel` passes, runs the policy before any write reaches GitHub, and holds at **AWAITING_HUMAN** until you `warden review approve`.

## Runbook \{#runbook\}

### 1. Start the stack

```bash
make up
```

`make up` starts Postgres, runs migrations, then brings up engine and worker. The engine mounts `config/` for policies, schemas, compensations, and prompts; the worker mounts prompts and the Docker socket for MCP.

If services look stuck, run `make doctor`.

### 2. Deploy manifests

```bash
export ENGINE_URL=http://127.0.0.1:8000

warden deploy -f config/worker.github-demo.yaml
warden deploy -f config/saga.github-demo.yaml
```

Deploy the worker before the saga.

### 3. Start a saga instance

```bash
warden start saga \
  -n github-demo \
  -v 0.1.0 \
  --namespace default \
  --input '{"owner":"your-org","repo":"your-repo"}'
```

Copy `<TRACE_ID>` as before. The commit step writes a comment to a real issue after approval — use a repo and issue you're willing to comment on. Pass an optional `issue_number` to bias triage toward a specific open issue.

### 4. Check triage status

```bash
warden list steps --trace-id <TRACE_ID> --namespace default
```

Triage often finishes in seconds — a one-shot `list steps` after start is usually enough. If `triage` is still `IN_PROGRESS`, run the command again or use `warden show step <TRACE_ID> --step-id triage --namespace default`. Add `--watch` to poll instead of re-running the command ([Start and monitor](../guides/cli/start-and-monitor.md)).

See [Observability](../guides/observability.md) for SQL and trace correlation after the run.

Step `triage` runs read-only tools: `get_me`, `list_issues`, and `issue_read` when issues exist. On completion, saga context gains:

- `steps.triage.output.data` — the `_submit` payload: `summary`, `recommended_issue_number`, and `comment_body` (`null` when empty)
- `steps.triage.facts.triage_metrics.total_count` — extracted from `list_issues` by the engine, not the LLM

### 5. HITL review

When `total_count > 0`, `post-comment` schedules. The engine evaluates `github-issue-comment.yaml` before pausing at `AWAITING_HUMAN` — the worker does not post until you approve. Command reference: [HITL review](../guides/cli/hitl-review.md).

If the repo has no open issues, `post-comment` is **SKIPPED** — jump to step 6.

```bash
warden review list --namespace default
```

Copy `saga_trace_id` and `step_span_id` for `post-comment` from the table (or filter with `--trace-id`). The table does not print the comment body — add `--json` to inspect what GitHub would receive before you approve:

```bash
warden review list --namespace default --trace-id <TRACE_ID> --json
warden show step <TRACE_ID> --step-id post-comment --namespace default --json
```

:::note
On commit steps, `review_payload` in the review list matches `resolved_arguments` on the step row: `owner`, `repo`, `issue_number`, and `body`.
:::

```bash
# Approve — queues DO_COMMIT
warden review approve <TRACE_ID> <STEP_SPAN_ID> --namespace default

# Reject — fails the step without calling GitHub
warden review reject <TRACE_ID> <STEP_SPAN_ID> --namespace default
```

#### Retry with guidance

While `post-comment` is still `AWAITING_HUMAN`, you can send the step back through the worker with operator notes instead of approving or rejecting:

```bash
warden review retry <TRACE_ID> <STEP_SPAN_ID> --namespace default \
  --guidance "Shorten the comment to two sentences; mention only the highest-priority issue."
```

Warden re-queues the step, merges your text into `_hitl_retry.guidance` on the worker arguments, and runs the same policy and HITL gates again. That pattern matters most on **reason** steps where the LLM should revise its draft; on this demo’s **commit** step, approve or reject is usually enough. Full command reference: [HITL review — HITL retry](../guides/cli/hitl-review.md#hitl-retry-operator-re-run).

If a step **failed** (for example `triage` after a bad PAT or MCP error), fix the environment and use [saga recovery](../guides/cli/saga-recovery.md) instead — `warden saga retry-step` does not take operator guidance.

### 6. Verify completion

```bash
warden list sagas --trace-id <TRACE_ID> --namespace default
warden list steps --trace-id <TRACE_ID> --namespace default
```

**If you approved HITL**, expect both steps and the saga to reach `COMPLETED` — then check GitHub for the triage comment.

**If you rejected HITL**, `triage` stays `COMPLETED`, `post-comment` lands `FAILED` (with `HUMAN_REJECTED` in `error_details`), and the saga ends `COMPENSATED` — no comment on GitHub. Triage was read-only, so Warden had nothing to undo; the saga still records that you blocked the write.

**With no open issues**, `triage` still `COMPLETED`, but `post-comment` is `SKIPPED` and the saga `COMPLETED` without any write to GitHub.

## What just happened

If you ran the demo, you saw Warden bound a high-risk GitHub write inside a predictable safety container:

1. **Deterministic branching from tool output** — Warden extracted `total_count` from the raw `list_issues` MCP JSON and evaluated `when.cel` to skip the commit step when the count is zero.
2. **Pre-flight guardrails** — When `post-comment` scheduled, Warden ran `before_commit` CEL on resolved arguments before any request reached GitHub.
3. **Human gate (HITL)** — Warden paused at **AWAITING_HUMAN** until you approved or rejected the write.

Tool facts, conditional branches, pre-flight policy, and manual override in one transactional loop — that is what separates Warden from a script that calls an LLM in process memory.

## Cleanup and repeat runs

Warden has no dry-run mode. When you approved HITL, Warden queued and executed a live `add_issue_comment` against your repository. Each approved run adds another comment on the recommended issue.

To run the demo again and exercise different paths:

- **Skip path, no network writes** — Start the saga against a repo with zero open issues. Warden evaluates `when.cel`, sees a zero count, and marks `post-comment` as `SKIPPED` with no side effects.
- **HITL reject, no GitHub write** — Let the saga pause at HITL, then run `warden review reject …`. You exercise the full governance loop and halt the forward path without posting a comment.
- **Live mutations on a safe target** — Use a dedicated test repo or personal fork you can litter with triage notes.

To remove comments after a live run, delete them in the GitHub issue UI. Warden tracks orchestration state in Postgres; it does not reach back out to undo external platform history.

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `MCP_UNAVAILABLE` / `Connection closed`; worker logs show `GITHUB_PERSONAL_ACCESS_TOKEN not set` | Set `GITHUB_PERSONAL_ACCESS_TOKEN` in `.env`, restart the worker (`docker compose up -d worker`), retry the step. `warden list steps --errors` should now name the missing variable instead of only listing tools. Confirm with `docker compose exec worker printenv GITHUB_PERSONAL_ACCESS_TOKEN` |
| Worker can't connect to MCP | Rebuild worker after Dockerfile changes: `docker compose build --no-cache worker && docker compose up -d worker`; then `docker compose exec worker /usr/bin/docker --version`. On host: `docker ps`; pull `ghcr.io/github/github-mcp-server`; verify `/var/run/docker.sock` is mounted on the worker service |
| `Policy artifact not found` | Engine must see policies — Compose engine mounts `config/policies`; confirm `github-issue-comment.yaml` exists |
| `Prompt uses variable(s) not defined` | Saga `with` keys must match `github-triage.j2`; prompts mounted at `/app/prompts` |
| `OUTPUT_SCHEMA_VALIDATION_FAILED` | Triage `_submit` must include `summary`, `recommended_issue_number`, and `comment_body`; use JSON `null` for the latter two when the repo has no open issues |
| `FACT_EXTRACTION_FAILED` | Tool output was not JSON or JSONPath missed — wrong repo often surfaces as `tool_result_preview` with `failed to list issues: …`; use `warden list steps --trace-id … --errors` |
| `TOOL_RESULT_TRUNCATED` | Defensive: tool JSON was cut at the historical 8000-char record limit (should not occur after worker fix); narrow MCP query params |
| `no_submit_call` | ReAct step exhausted turns without `_submit` — check `last_tool_errors` in `warden show step … --json` for MCP messages |
| `issue_read` 404 | Don't pass `issue_number` for a closed or missing issue |
| `POLICY_COMMIT_DENIED` | Commit `body` must contain `## Warden triage`; `owner`/`repo` must match start input |
| HITL approve queued but saga stuck | `make doctor`, then re-run `warden review approve` to requeue the outbox row |
| Missing API key for `openai` | Set `OPENAI_API_KEY` in `.env`, restart the worker (`docker compose up -d worker`), retry or start a new saga — or switch the manifest to `provider: local` (`WARDEN_LOCAL_LLM_BASE_URL`) or `provider: mock` |
| Tool not in allowlist | Step `tools.allow` names must match GitHub MCP tool IDs exactly |
| Slow / costly triage on large repos | `list_issues` JSON is clipped for LLM turns (default 8000 chars) but facts use the full payload; tune `WARDEN_REACT_TOOL_MESSAGE_LIMIT` or set `0` to disable clipping for debugging — see [Configuration → Worker tuning](configuration.md#worker-tuning) |

## What's next

You have finished the getting-started demos. [Configuration](configuration.md) catalogs the env vars you touched (`GITHUB_PERSONAL_ACCESS_TOKEN`, `OPENAI_API_KEY`, artifact roots).

The GitHub walkthrough is rigid on purpose so the safety boundaries are easy to see. When you author your own saga, start from the patterns in `config/` and go deeper in the Guides — [Manifests and artifacts](../guides/manifests/overview.md), [Policies](../guides/manifests/policies.md), [HITL review](../guides/cli/hitl-review.md).

## Related

- [Demo: Quickstart](demo-quickstart.md)
- [Demo: Mock LLM and MCP](demo-mock-llm-and-mcp.md)
- [Saga manifests](../guides/manifests/saga-manifests.md) — `when`, `facts`, reason vs commit
- [MCP and tools](../guides/manifests/mcp-and-tools.md)
- [Policies](../guides/manifests/policies.md)
- [HITL review](../guides/cli/hitl-review.md)
