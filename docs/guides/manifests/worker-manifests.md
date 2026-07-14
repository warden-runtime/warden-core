---
sidebar_position: 2
pagination_prev: guides/manifests/overview
pagination_next: guides/manifests/saga-manifests
---

# Worker manifests

A worker manifest tells Warden which LLM to use and which MCP servers to connect to. Run `warden deploy -f config/<worker-manifest>.yaml` to save the configuration in Postgres.

Keep in mind that deploying this manifest doesn't start a worker process. It saves the config to the database. When your running worker pulls a task from the outbox, it reads this saved definition to figure out how to execute the step.

This page covers required fields, providers, and MCP setup. Saga steps point to a worker by matching its name and version. We'll connect them in the next guide on [Saga manifests](saga-manifests.md).

## Required fields

Every worker manifest needs identity, model, and a system prompt:

```yaml
kind: worker
namespace: default
name: my-worker
version: 0.1.0
provider: openai
model_name: gpt-4o
system_prompt: |
  You are a helpful assistant.
```

`name`, `namespace`, and `version` identify the saved worker (see [Manifests and artifacts → Deploy and identity](overview.md#deploy-and-identity)). Saga steps reference it with `worker` + `worker_version`. Steps use the saga's `namespace` — you don't set namespace on the step itself.

## Providers

Warden currently supports four inference providers:

| Provider | Credentials | Notes |
|----------|-------------|-------|
| `openai` | `OPENAI_API_KEY` (or `provider_secrets` row) | Any OpenAI-compatible cloud model |
| `anthropic` | `ANTHROPIC_API_KEY` (or `provider_secrets` row) | Claude models via LangChain |
| `local` | None — optional `WARDEN_LOCAL_LLM_BASE_URL` | OpenAI-compatible local endpoint (Ollama, vLLM, etc.). Under Compose, see [Configuration → Local LLM under Docker (Ollama)](../../getting-started/configuration.md#local-llm-under-docker-ollama) |
| `mock` | None | Credential-free demo — [Demo: Mock LLM and MCP](../../getting-started/demo-mock-llm-and-mcp.md) |

Unknown `provider` values fail at worker step runtime with `ValueError` from `build_llm()`. To add another provider, see [Extending Warden — LLM providers](../../advanced/extending-warden.md#add-an-llm-provider).

## MCP tool sources

Warden workers use the [Model Context Protocol (MCP)](https://modelcontextprotocol.io) to talk to external APIs. List the servers your agent can reach under `tool_sources` in the worker manifest; saga steps narrow that list with their own `tools.allow`.

When a worker picks up a step, it opens a connection for each source. For **`stdio`** sources, that means spawning a subprocess that stays alive until the step finishes or times out — stdin/stdout carry messages between the MCP server and your agent loop. For **`sse`** sources, the worker connects over HTTP to a server that's already running elsewhere.

| Transport | Config | How it connects |
|-----------|--------|-----------------|
| `sse` (default) | `url` | HTTP SSE client to a running MCP server |
| `stdio` | `command`, `args` | Spawns a subprocess; MCP speaks over stdin/stdout |

**SSE** — for an MCP server your worker reaches over the network (Compose service, k8s sidecar, hosted endpoint):

```yaml
tool_sources:
  - name: my-mcp
    transport: sse
    url: http://mcp-service:8765/sse
    headers:
      Authorization: "Bearer ${ENV:COMPANY_MCP_TOKEN}"
      X-Api-Key: "${ENV:GATEWAY_KEY}"
```

Set `COMPANY_MCP_TOKEN` and `GATEWAY_KEY` on the worker service (`.env` or Compose `env_file`) — not in the manifest. Literal header values work too when you skip `${ENV:…}` placeholders. The worker needs network access to the URL.

**Stdio** — for a local process the worker starts per connection (a binary on disk or `docker run`, as in the GitHub demo):

```yaml
tool_sources:
  - name: github
    transport: stdio
    command: docker
    args:
      - run
      - --rm
      - -i
      - -e
      - GITHUB_PERSONAL_ACCESS_TOKEN
      - ghcr.io/github/github-mcp-server
    env_inherit:
      - GITHUB_PERSONAL_ACCESS_TOKEN
```

Pass secrets on the worker service (`.env`), not in the manifest. Use `env:` for explicit values, `env_inherit:` to copy names from the worker process, or `docker run -e VAR` in `args`. See the [GitHub MCP demo](../../getting-started/demo-github-mcp.md) for a Docker stdio example on the dev stack.

Omit `tool_sources` if the worker doesn't need MCP tools.

## Optional fields

Everything beyond the [required fields](#required-fields) is optional. Common additions:

| Field | Default | Purpose |
|-------|---------|---------|
| `description` | — | Short note for your team or deploy listings |
| `temperature` | `0.0` | LLM sampling temperature |
| `tool_sources` | `[]` | MCP servers — see [MCP tool sources](#mcp-tool-sources) |
| `adapter` | `langchain` | How the worker runs agent steps internally. Leave at default unless you ship a custom adapter. **Not** the same as saga-step `agent-adapter: react \| simple` — see [Saga manifests → Reason step execution](saga-manifests.md#reason-step-execution-agent-adapter) |
| `compensation_prompt` | Built-in compensation prompt | **Multi-tool compensation only** — ignored when compensation YAML has exactly one `tools.allow` entry |

### Compensation prompt

The usual path is **one tool** in your compensation YAML's `tools.allow`. The worker calls it once — no LLM, same as a commit step. `compensation_prompt` is ignored on that path.

Use `compensation_prompt` only when compensation allows **multiple** tools and needs a ReAct loop. The worker prepends this text before that loop (or a built-in default if the field is empty). `max_turns` on the compensation file caps the loop.

Even with a custom prompt, the engine's core safety rules still apply — your agent won't auto-retry or diagnose errors during an active rollback. For most workflows, skip `compensation_prompt` and stick to a single, direct undo tool. See [Compensation](compensation.md).

### How many times the agent can loop (`max_turns`)

This setting actually lives on each individual **`kind: reason` step** inside your saga YAML — not here on the worker manifest.

For a standard **`react`** step, your agent will keep calling tools and talking to the LLM until it decides to run the special `_submit` tool. `max_turns` acts as a safety valve to cap those back-and-forth rounds (default is **10**, max is **200**). If you use a **`simple`** step, it only makes a single LLM call and ignores this cap entirely.

Most of your undo paths will just call a single MCP tool without any agent logic at all. But if you have a complex, multi-tool undo sequence that needs a short reasoning loop, you can set a custom `max_turns` value inside your compensation YAML file too.

See [Saga manifests → Step budgets](saga-manifests.md#step-budgets) for defaults and examples.

## What's next

Next up: [Saga manifests](saga-manifests.md) — connect this worker to your steps, set tool allowlists, and add policy and HITL gates.
