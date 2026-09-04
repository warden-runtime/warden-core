---
sidebar_position: 4
pagination_prev: guides/manifests/saga-manifests
pagination_next: guides/manifests/mcp-and-tools
---

# Prompts

A `reason` step uses a [Jinja2](https://jinja.palletsprojects.com/en/stable/templates/) template to build the user prompt that goes to the LLM. Prompt **files** live on disk under `PROMPTS_ROOT`; your **step** manifest points at a relative path (`prompt: triage.j2`). At saga **start**, the engine freezes the template (with static `{% include %}` inlined) onto the instance as `prompt_definition`. The worker renders that frozen string with resolved `with` bindings — it does not re-read `PROMPTS_ROOT` mid-run.

Commit steps never use prompt files — they call one MCP tool with resolved `with` arguments. Compensation undo steps use YAML under `COMPENSATIONS_ROOT` (not saga prompt templates) — see [Compensation](compensation.md).

For how `with` bindings work, see [Saga manifests → Bindings](saga-manifests.md#bindings-with) and [Step manifests](step-manifests.md).

## Referencing a prompt from a step

Set `prompt` on a **reason** [step manifest](step-manifests.md). Every value the template reads must be declared under the step's `inputs` (and bound from the saga `with` block — see [Template context](#template-context)):

```yaml
# step catalog
kind: step
name: analyze
version: "1.0.0"
step_kind: reason
worker: analyst-worker
worker_version: "1.0.0"
prompt: analyze.j2
inputs:
  repo:
    required: true
```

```yaml
# saga composition
steps:
  - id: analyze
    use: analyze
    version: "1.0.0"
    with:
      repo:
        from: $.input.repo
```

When you deploy the **step** manifest, Warden checks your `{PROMPTS_ROOT}/analyze.j2` file early. The engine blocks deploy if:

- `PROMPTS_ROOT` isn't configured on the engine
- The prompt file can't be found at that path
- The template references a `{{ variable }}` that isn't declared in the step's `inputs`

The `prompt` value must be a **relative path** under `PROMPTS_ROOT` — no leading `/` and no `..` segments.

When the template has no `{{ variables }}`, use an empty `inputs: {}` map (see [Demo: Quickstart](../../getting-started/demo-quickstart.md)):

```yaml
prompt: noop.j2
inputs: {}
```

For JSONPath syntax, resolution timing, and binding to prior step output, see [Saga manifests → Bindings](saga-manifests.md#bindings-with).

## Where prompt files live

Prompt **refs** stay as paths on disk. At saga start the engine copies the resolved template text onto `frozen_steps` / the step row as `prompt_definition` (same freeze pattern as policy and compensation). Disk edits after start do not affect **running** instances:

| Consumer | When | What |
|----------|------|------|
| Engine | Step registration | Read file body; validate `{{ var }}` ⊆ step `inputs` keys |
| Engine | Saga start / child spawn | Freeze template (+ static includes) into `prompt_definition` |
| Worker | Step execution | Render frozen `prompt_definition` with resolved bindings |

The **engine** needs `PROMPTS_ROOT` at register and start. Workers do not re-read prompt files for step execution. In Compose, `./config/prompts` mounts at `/app/prompts` on the engine — leave `PROMPTS_ROOT` unset in `.env` so container paths win. On the host CLI, export `PROMPTS_ROOT=./config/prompts`. See [Manifests and artifacts](overview.md) and [Configuration → Disk artifact roots](../../getting-started/configuration.md#disk-artifact-roots).

When `PROMPTS_ROOT` is set, the engine fails fast at startup if the path is not a readable directory.

## Deploy-time validation

When you deploy a saga, the engine reads each prompt file and checks **`{{ ... }}` expressions**. Every top-level name in the template must have a matching key in `with`:

| Template | Required `with` key |
|----------|---------------------|
| `Hello {{ name }}` | `name` |
| `Owner: {{ user.email }}` | `user` (object; nested access is Jinja on the bound value) |
| `{% if focus_issue_number is not none %}…{% endif %}` | not checked statically — bind `focus_issue_number` anyway |
| `{% for obj in objectives %}{{ obj }}{% endfor %}` | `objectives` (loop target `obj` and Jinja `loop` are allowed) |

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
- **`allowed_tools`** — you don't declare this under `with`. On **`react`** steps, the worker injects it automatically: **sanitized** MCP tool names bound for the step (provider-safe form of each allowed tool), plus `read_resource` when `resources.allow` is set, plus `_submit`. You can use `{{ allowed_tools }}` in the template to list what the agent can call. Not present on **`simple`** steps (no tool loop). See [MCP and tools → Tool allowlists](mcp-and-tools.md#tool-allowlists).

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

1. Read the start-frozen `prompt_definition` from the step row (already includes static `{% include %}` text).
2. Render that template with resolved `with` values + `allowed_tools`.
3. Send the worker manifest's **`system_prompt`** as the system message.
4. Send the **rendered step prompt** (the Jinja output from step 2) as the human message. String templates (Jinja / inline) are sent as **plain text**. Structured dict/list prompt inputs are JSON-encoded so the model still receives a parseable object.

Editing prompt files on disk does **not** change already-started saga instances — they keep the freeze from start. New starts pick up the current files (after you redeploy the step if registration validation must see new `{{ variables }}`). Registration already validated variable names against step `inputs`.

If you add new `{{ variables }}`, update the step's `inputs` / saga `with` and bump the **step** `version` (append-only), then update saga `use:` pins. Prefer a new saga `version` in production when composition changes so new runs pick up the contract and **running sagas** keep the freeze they started with. See [Manifests and artifacts → Deploy and identity](overview.md#deploy-and-identity).

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

## Includes (`{% include %}`)

File prompts under `PROMPTS_ROOT` support Jinja [`{% include %}`](https://jinja.palletsprojects.com/en/stable/templates/#include). At saga **start**, the engine inlines **bare** static string includes (`{% include 'partial.j2' %}`) into `prompt_definition` via the Jinja lexer/AST — includes inside `{# comments #}` and `{% raw %}` are left intact. Dynamic includes and modifiers (`ignore missing`, `with context`, `without context`) are rejected at freeze. Partials must stay under the same root — absolute paths and `..` segments are rejected. Include cycles fail freeze.

Rendering at execute time uses Jinja’s [`SandboxedEnvironment`](https://jinja.palletsprojects.com/en/stable/api/#jinja2.sandbox.SandboxedEnvironment) on the frozen string (no disk loader): attribute escapes / SSTI-style constructs are blocked, and built-in helpers that expose object graphs (`cycler`, `joiner`, `namespace`, `lipsum`) are removed. Keep templates to variables, filters, conditionals, loops, and static includes.

```jinja
{# analyze.j2 #}
{% include 'partials/profile.j2' %}

Summarize the claim for {{ claim_id }}.
```

```jinja
{# partials/profile.j2 #}
Standing profile: {{ profile_summary }}
```

Inline / `with` string templates still use a string Jinja loader and do **not** resolve includes — only prompt **files** under `PROMPTS_ROOT` are expanded at start freeze. Both paths share the same sandbox.

## The `noop` prompt

The minimal saga uses a one-line smoke-test template to verify engine registration, worker hydration, and LLM wiring before authoring real instructions. See [Demo: Quickstart](../../getting-started/demo-quickstart.md).

## Runtime troubleshooting

| Symptom | Likely fix |
|---------|------------|
| Registration 400: prompt file not found | Engine `PROMPTS_ROOT` or mount; see [Troubleshooting](../../getting-started/troubleshooting.md) |
| Start fails: `prompts_root is not configured` / freeze error | Set/mount `PROMPTS_ROOT` on the **engine** before saga start |
| Step fails: missing `prompt_definition` | Instance was created before prompt freeze; restart the saga after upgrading |
| Step fails: `Jinja render failed` | Missing `with` key or wrong type at schedule time (often a JSONPath to a step that has not completed) |
| Step fails: `Jinja render blocked unsafe construct` | Template used a sandboxed-forbidden attribute or helper; remove SSTI-style / introspection syntax |
| Agent ignores tools | Check `tools.allow` on the step and worker MCP config — not the prompt file alone |

## What's next

Next up: [MCP and tools](mcp-and-tools.md) — configure transports, tool allowlists, and how workers connect to external APIs.

## Related

- [Jinja2 template designer documentation](https://jinja.palletsprojects.com/en/stable/templates/) — syntax for variables, conditionals, loops, and filters
- [Saga manifests](saga-manifests.md) — reason vs commit, `with`, `tools.allow`, `facts`
- [Worker manifests](worker-manifests.md) — `system_prompt`, MCP `tool_sources`
- [Configuration](../../getting-started/configuration.md) — host vs Compose `PROMPTS_ROOT`
- [Demo: Quickstart](../../getting-started/demo-quickstart.md) — minimal saga with `noop.j2`
