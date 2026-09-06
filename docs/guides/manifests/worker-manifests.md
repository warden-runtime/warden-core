---
sidebar_position: 2
pagination_prev: guides/manifests/overview
pagination_next: guides/manifests/step-manifests
---

# Worker manifests

A worker manifest tells Warden which LLM to use and which MCP servers to connect to. Run `warden deploy -f config/<worker-manifest>.yaml` to save the configuration in Postgres.

Keep in mind that deploying this manifest doesn't start a worker process. It saves the config to the database. When your running worker pulls a task from the outbox, it reads this saved definition to figure out how to execute the step.

This page covers required fields, providers, and MCP setup. [Step manifests](step-manifests.md) pin a worker by name and version; sagas compose those steps.

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

`name`, `namespace`, and `version` identify the saved worker (see [Manifests and artifacts → Deploy and identity](overview.md#deploy-and-identity)). Step manifests reference it with `worker` + `worker_version` in the same namespace.

## Providers

Warden currently supports five inference providers:

| Provider | Credentials | Notes |
|----------|-------------|-------|
| `openai` | `OPENAI_API_KEY` (or `provider_secrets` row) | Any OpenAI-compatible cloud model |
| `anthropic` | `ANTHROPIC_API_KEY` (or `provider_secrets` row) | Claude models via LangChain |
| `azure` | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` (or `provider_secrets` for the key) | Azure OpenAI / Microsoft Foundry via OpenAI-compatible `/openai/v1/` (Chat Completions by default for prompt caching; optional Responses via `WARDEN_AZURE_USE_RESPONSES_API`); copy host+key from the deployment Code sample; `model_name` is the deployment name |
| `local` | None — optional `WARDEN_LOCAL_LLM_BASE_URL` | OpenAI-compatible local endpoint (Ollama, vLLM, etc.). Under Compose, see [Configuration → Local LLM under Docker (Ollama)](../../getting-started/configuration.md#local-llm-under-docker-ollama) |
| `mock` | None | Credential-free demo — [Demo: Mock LLM and MCP](../../getting-started/demo-mock-llm-and-mcp.md) |

Unknown `provider` values fail at worker step runtime with `ValueError` from `build_llm()`. To add another provider, see [Extending Warden — LLM providers](../../advanced/extending-warden.md#add-an-llm-provider).

## MCP tool sources

Warden workers use the [Model Context Protocol (MCP)](https://modelcontextprotocol.io) to talk to external APIs. List the servers your agent can reach under `tool_sources` in the worker manifest; step manifests narrow that list with their own `tools.allow`.

When a worker picks up a step, it opens a connection for each source. For **`stdio`** sources, that means spawning a subprocess that stays alive until the step finishes or times out — stdin/stdout carry messages between the MCP server and your agent loop. For **`streamable_http`** sources, the worker connects over Streamable HTTP to a server that's already running elsewhere.

| Transport | Config | How it connects |
|-----------|--------|-----------------|
| `streamable_http` (default) | `url` | Streamable HTTP client to a running MCP server |
| `stdio` | `command`, `args` | Spawns a subprocess; MCP speaks over stdin/stdout |

**Streamable HTTP** — for an MCP server your worker reaches over the network (Compose service, k8s sidecar, hosted endpoint). The URL must target a [Streamable HTTP](https://modelcontextprotocol.io) endpoint (commonly `/mcp`), not an SSE `/sse` path:

```yaml
tool_sources:
  - name: my-mcp
    transport: streamable_http
    url: http://mcp-service:8765/mcp
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
| `temperature` | `0.0` | LLM sampling temperature (persisted on the worker definition `body` and passed to the model at runtime) |
| `tool_sources` | `[]` | MCP servers — see [MCP tool sources](#mcp-tool-sources) |
| `adapter` | `langchain` | How the worker runs agent steps internally. Leave at default unless you ship a custom adapter. **Not** the same as step-manifest `agent-adapter: react \| simple` — see [Saga manifests → Reason step execution](saga-manifests.md#reason-step-execution-agent-adapter) |

### How many times the agent can loop (`max_turns`)

This setting lives on each **`step_kind: reason`** step manifest — not on the worker.

For a standard **`react`** step, the agent keeps calling tools until it invokes `_submit`. `max_turns` caps those rounds (default **25**, max **200**). Invalid `_submit` payloads that trigger schema soft-retries still draw from this budget — see [Configuration → LLM schema soft-retries](../../getting-started/configuration.md#llm-schema-soft-retries-validation-feedback). **`simple`** steps make one LLM call and ignore this cap.

Compensation undo is a single MCP tool call (see [Compensation](compensation.md)); it does not use `max_turns`.

See [Saga manifests → Step budgets](saga-manifests.md#step-budgets) for defaults and examples.

## What's next

Next up: [Step manifests](step-manifests.md) — declare reusable reason/commit capabilities that pin this worker, then compose them in [Saga manifests](saga-manifests.md).
