---
sidebar_position: 4
pagination_prev: guides/manifests/saga-manifests
pagination_next: guides/manifests/mcp-and-tools
---

# Prompts

A `reason` step uses a [Jinja2](https://jinja.palletsprojects.com/en/stable/templates/) template to build the user prompt that goes to the LLM. Unlike saga manifests, prompts aren't saved to the database with version numbers — they live as plain files on disk under `PROMPTS_ROOT`. Your saga manifest points to the file by name (like `prompt: triage.j2`) and uses a `with` block to feed data into it. When the step runs, the worker combines the file and the data to render your prompt.

Commit steps never use prompt files — they call one MCP tool with resolved `with` arguments. Compensation undo steps use YAML under `COMPENSATIONS_ROOT` (and optionally the worker's `compensation_prompt`), not saga prompt templates — see [Compensation](compensation.md).

For how `with` bindings work, see [Saga manifests → Bindings](saga-manifests.md#bindings-with).

## Referencing a prompt from a step

Set `prompt` on any `reason` step. Every value the template reads must be declared under `with` (or injected by the worker — see [Template context](#template-context)):

```yaml
steps:
  - id: analyze
    kind: reason
    worker: analyst-worker
    prompt: analyze.j2
    with:
      repo:
        from: $.input.repo
```

When you deploy your manifest, Warden checks your `{PROMPTS_ROOT}/analyze.j2` file early to make sure everything works. The engine will catch errors and block the deploy if:

- `PROMPTS_ROOT` isn't configured on the engine
- The prompt file can't be found at that path
- The template references a `{{ variable }}` that isn't declared in `with`

The `prompt` value must be a **relative path** under `PROMPTS_ROOT` — no leading `/` and no `..` segments.

When the template has no `{{ variables }}`, you can **omit `with` entirely** — the engine treats a missing block the same as an empty map. The [Demo: Quickstart](../../getting-started/demo-quickstart.md) minimal saga uses an explicit empty map for clarity:

```yaml
prompt: noop.j2
# with: {}   # optional — omit when the template has no variables
```

For JSONPath syntax, resolution timing, and binding to prior step output, see [Saga manifests → Bindings](saga-manifests.md#bindings-with).

## Where prompt files live

Prompt files stay on disk — they aren't copied into Postgres. Both engine and worker read them from `PROMPTS_ROOT` at different times:

| Consumer | When | What |
|----------|------|------|
| Engine | Saga registration | Read file body; validate `{{ var }}` ⊆ `with` keys |
| Engine | Step schedule | Re-check file still exists |
| Worker | Step execution | Load file body; render with resolved bindings |

Both engine and worker need the same logical tree. In Compose, `./config/prompts` mounts at `/app/prompts` on both services — leave `PROMPTS_ROOT` unset in `.env` so container paths win. On the host CLI, export `PROMPTS_ROOT=./config/prompts`. See [Manifests and artifacts](overview.md) and [Configuration → Disk artifact roots](../../getting-started/configuration.md#disk-artifact-roots).

When `PROMPTS_ROOT` is set, engine and worker fail fast at startup if the path is not a readable directory.

## Deploy-time validation

When you deploy a saga, the engine reads each prompt file and checks **`{{ ... }}` expressions**. Every top-level name in the template must have a matching key in `with`:

| Template | Required `with` key |
|----------|---------------------|
| `Hello {{ name }}` | `name` |
| `Owner: {{ user.email }}` | `user` (object; nested access is Jinja on the bound value) |
| `{% if focus_issue_number is not none %}…{% endif %}` | not checked statically — bind `focus_issue_number` anyway |

Extra `with` keys are allowed. Variables used only in `{% if %}`, `{% for %}`, or filters are **not** checked when you deploy; if you reference them at render time without a binding, the step fails in the worker.

Common registration errors:

| Symptom | Cause |
|---------|--------|
| `prompts_root is not configured` | Engine has no `PROMPTS_ROOT` while a reason step sets `prompt` |
| `Prompt file not found: …` | File missing or wrong root on the **engine** |
| `Prompt uses variable(s) not defined in step 'with': …` | `{{ var }}` in template with no matching `with` key |
| `Invalid prompt …` / `escapes PROMPTS_ROOT` | Absolute path or `..` in the `prompt` field |

## Template context

Bindings are resolved right before the step runs (JSONPath against saga context). The worker gets a flat map and builds the Jinja context:

- Each `with` key becomes a top-level template variable (`repo` → `{{ repo }}`).
- **`allowed_tools`** — you don't declare this under `with`. On **`react`** steps, the worker injects it automatically: MCP tool IDs from `tools.allow`, plus `read_resource` when `resources.allow` is set, plus `_submit`. You can use `{{ allowed_tools }}` in the template to list what the agent can call. Not present on **`simple`** steps (no tool loop).

Templates don't get the full `steps.*` tree. To use a prior step's output, bind it explicitly:

```yaml
with:
  summary:
    from: $.steps.triage.output.data.summary
```

### Nested objects in templates

Bind the **top-level object** under `with`; Jinja handles nested keys. The engine only checks that `user` appears in `with` when the template contains `{{ user.email }}`.

Prior reason-step structured output (stored at `steps.assign.output.data`):

```json
{
  "user": { "email": "ada@example.com", "name": "Ada" }
}
```

Saga step bindings and template:

```yaml
with:
  user:
    from: $.steps.assign.output.data.user
prompt: notify.j2
```

```jinja
Owner: {{ user.email }} ({{ user.name }})
```

## How the worker renders the prompt

The worker renders the prompt when the step runs — not when you deploy the saga.

1. Load `{PROMPTS_ROOT}/<prompt>` from disk (fresh read each run) — the step's prompt template file from the saga manifest `prompt` field.
2. Render that template with resolved `with` values + `allowed_tools`.
3. Send the worker manifest's **`system_prompt`** as the system message.
4. Send the **rendered step prompt** (the hydrated `.j2` output from step 2) as the human message. On **`react`** steps it is JSON-encoded for the ReAct loop; on **`simple`** steps it is sent as plain text for a single structured completion.

You can edit prompt files on disk without redeploying the saga manifest. Registration already validated variable names; content changes apply on the next step run for any instance that references that `prompt` filename.

If you add new `{{ variables }}`, update the saga step's `with` block and redeploy. You don't have to bump `version` for deploy to succeed — the engine upserts the same `(namespace, name, version)` in place. In development, redeploying the same version is usually fine. In production, prefer a new saga `version` when `with` or other step fields change so new runs pick up the contract and **running sagas** keep the bindings they started with. See [Manifests and artifacts → Deploy and identity](overview.md#deploy-and-identity).

For Jinja syntax (conditionals, loops, filters), see the [Jinja template designer docs](https://jinja.palletsprojects.com/en/stable/templates/).

## Example: triage prompt

A typical reason-step template lists inputs from `with` and documents tool order:

```jinja
## Target repository
- **owner:** {{ owner }}
- **repo:** {{ repo }}
{% if focus_issue_number is not none %}
- **focus issue (preferred):** #{{ focus_issue_number }}
{% endif %}
```

Matching saga bindings:

```yaml
with:
  owner:
    from: $.input.owner
  repo:
    from: $.input.repo
  focus_issue_number:
    from: $.input.focus_issue_number
```

The full GitHub demo template documents the **`react`** `_submit` JSON contract — see [GitHub MCP demo](../../getting-started/demo-github-mcp.md). The [Quickstart](../../getting-started/demo-quickstart.md) uses **`simple`** with `noop.j2` (no `_submit` instructions).

## The `noop` prompt

The minimal saga uses a one-line smoke-test template to verify engine registration, worker hydration, and LLM wiring before authoring real instructions. See [Demo: Quickstart](../../getting-started/demo-quickstart.md).

## Runtime troubleshooting

| Symptom | Likely fix |
|---------|------------|
| Registration 400: prompt file not found | Engine `PROMPTS_ROOT` or mount; see [Troubleshooting](../../getting-started/troubleshooting.md) |
| Worker: `prompts_root is not configured` | Set/mount `PROMPTS_ROOT` on the **worker** service |
| Step fails: `Jinja render failed` | Missing `with` key or wrong type at schedule time (often a JSONPath to a step that has not completed) |
| Agent ignores tools | Check `tools.allow` on the step and worker MCP config — not the prompt file alone |

## What's next

Next up: [MCP and tools](mcp-and-tools.md) — configure transports, tool allowlists, and how workers connect to external APIs.

## Related

- [Jinja2 template designer documentation](https://jinja.palletsprojects.com/en/stable/templates/) — syntax for variables, conditionals, loops, and filters
- [Saga manifests](saga-manifests.md) — reason vs commit, `with`, `tools.allow`, `facts`
- [Worker manifests](worker-manifests.md) — `system_prompt`, MCP `tool_sources`
- [Configuration](../../getting-started/configuration.md) — host vs Compose `PROMPTS_ROOT`
- [Demo: Quickstart](../../getting-started/demo-quickstart.md) — minimal saga with `noop.j2`
