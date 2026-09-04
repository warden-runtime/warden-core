# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking

- Saga authoring no longer inlines reason/commit capability blobs. Deploy `kind: step` manifests first, then compose with `use:` / `version` / `with` / `when` (workers → steps → sagas). Migration `011_step_definitions.sql` adds `step_definitions` and pins `step_definition_name` / `step_definition_version` on step instances.
- Saga definitions store the **authoring AST** only. Deploy **link-checks** catalog refs (ports, tighten, artifacts) but does not persist an expanded reason/commit photocopy. At saga **start** (and child spawn), the engine hydrates `use:` refs into instance `frozen_steps`. Redeploy sagas after upgrading — expanded definition bodies are not accepted.
- Step and worker `(namespace, name, version)` pins are **append-only**. Redeploying the same version fails with an immutable-version error; bump the version to change capability.
- Worker `tool_sources` no longer support legacy `transport: sse`. Use `streamable_http` (the default when `transport` is omitted) with a Streamable HTTP endpoint URL (commonly `/mcp`). Deploy rejects `sse` with a clear validation error.
- Compensation YAML must declare **exactly one** tool in `tools.allow` (same as commit). Reason and commit steps share that single-tool undo path. Worker-manifest / DB `compensation_prompt` is removed (migration `008_drop_compensation_prompt.sql`).
- Removed `WARDEN_REACT_TOOL_MESSAGE_LIMIT` and all client-side MCP tool-output clipping. Tool payloads flow unchanged to the LLM transcript and `tool_results` / facts extraction. Tier-1 memory redaction (coupled to the clip limit) is removed; golden-ratio compression still digest/drops **historical** turns.
- Worker MCP client upgraded to **MCP Python SDK 2.x** (`mcp>=2.0,<3` on the worker extra). Rebuild worker images / re-run `uv sync --extra worker` after pulling. Client code uses v2 pagination (`PaginatedRequestParams`), streamable HTTP 2-tuple transports, and SDK snake_case protocol fields; the stdio mock fixture uses the v2 lowlevel `Server` handler API.
- Soft-disable catalog definitions moved from `PATCH /v1/definitions/{kind}/{uuid}` to `PATCH /v1/definitions/{kind}?id=` **or** `?namespace=&name=&version=` (CLI flags match).

### Added

- First-class **step manifests** (`kind: step`): catalog capabilities with input ports, `step_kind` reason|commit, and capability fields; sagas compose via `use:` + `version` + `with` + `when` (tighten-only overrides). Saga deploy link-checks refs; saga start freezes hydrated steps onto the instance. Input port `schema` fragments are enforced at step deploy (valid Draft-7), saga deploy (`value:` literals), and schedule (resolved bindings). List with `warden list definitions --type step` / `GET /v1/definitions/steps`. Docs: [Step manifests](docs/guides/manifests/step-manifests.md).
- Worker definitions store the full validated manifest in `body` JSONB (same pattern as sagas/steps). Migration `012_worker_definition_body.sql` backfills and drops denormalized `model_provider` / `model_name` / `system_prompt` / `tool_sources` / `adapter` columns. `GET .../workers/{id}?include_body=true` returns `row.body`. Worker `temperature` is persisted and applied at runtime.
- Catalog definition list/get items are uniform: `id`, `namespace`, `name`, `version`, `is_active`, timestamps (+ optional `body`). Workers gained `is_active` (`013_worker_is_active.sql`); top-level list `adapter` was removed (still in `body`). Soft-disable via `PATCH /v1/definitions/{workers|steps|sagas}` with query identity (`id` or triple) or `warden definitions set-active`. Inactive workers/steps/sagas are rejected at deploy link-check and runtime load with structured HTTP codes.
- Catalog vocabulary: executable saga graph type is `HydratedSagaBlueprint` (authoring remains `SagaAuthoringBlueprint`). Worker timing bucket `hydration_ms` renamed to `worker_init_ms`. Reserve **hydrate** for catalog→runtime graph resolution; Jinja uses **render**; worker command load uses **prepare**/`worker_init_ms`.
- Registry deploy stores definition bodies via one shared `_definition_body_payload` helper. Removed unused `get_saga_definition_by_id`, unreachable worker version-mismatch branch, and dead CLI list params; tests share `worker_definition_body` / `step_definition_body` factories.

- [MCP and tools → Tool payload hygiene](docs/guides/manifests/mcp-and-tools.md#tool-payload-hygiene) — pagination, field projection, and summary-tool patterns for bounded MCP returns.

- Reason-step `tools.bind` (⊆ `with`): pin saga-resolved values onto ReAct MCP tool args (saga wins, ∩ tool `inputSchema`), strip those keys from the LLM-facing schema, and persist `tools_bind` on step rows (migration `009_tools_bind.sql`). Rejected on commit, compensation, and `agent-adapter: simple`.
- `provider: azure` — Azure OpenAI / Microsoft Foundry via LangChain `ChatOpenAI` and the OpenAI-compatible `/openai/v1/` path (`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`; worker `model_name` is the deployment name). Defaults to Chat Completions for prompt-cache friendliness; Responses API is opt-in via `WARDEN_AZURE_USE_RESPONSES_API`.
- Submit-mode ReAct soft-feeds **recoverable** tool mismatches (e.g. `search_replace` `old_text not found` / non-unique match, missing path, patch-apply text failures) into the transcript with a one-line recovery hint instead of failing the step with `TOOL_OUTPUT_ERROR`. `apply_patch_sandbox` JSON rejects get the same hint. Transport/`MCP error` / invalid-argument failures remain hard. Override via `ToolLifecycleHooks.tool_output_is_recoverable`.
- Submit-mode ReAct softens **recoverable tool invoke exceptions** (e.g. `IsADirectoryError`, Pydantic arg validation) by normalizing them to `Error:` tool payloads and reusing the same recoverability classifier; MCP `CallToolResult.isError` is honored at the tool wrapper. Infrastructure failures (`MCPError`, connection/timeout, mixed `ExceptionGroup` with transport leaves) still raise `TOOL_INVOKE_FAILED`.

### Changed

- Definition list/get/patch routes share a generic CRUD helper; PATCH identity moved from path UUID to query `id` or `namespace`+`name`+`version` on `PATCH /v1/definitions/{workers|steps|sagas}`.
- Catalog inactive/missing errors use a shared exception hierarchy (`InactiveCatalogDefinitionError` / `CatalogDefinitionNotFoundError`) with structured HTTP detail payloads.

### Fixed

- Parent saga deploy rejects **inactive** child saga definitions referenced by `spawn_sagas` (must exist and `is_active`); inactive catalog refs map to HTTP **409** with structured `INACTIVE_CATALOG_DEFINITION` (missing → **404** `CATALOG_DEFINITION_NOT_FOUND`).
- Soft-disable catalog definitions via `PATCH /v1/definitions/{workers|steps|sagas}?id=` **or** `?namespace=&name=&version=` (CLI: `--id` XOR `--namespace/--name/--version`).
- Schedule-time input validation logs a warning when resolved arguments are present but `input_ports` is empty (no hard-fail for engine-native steps).
- Child spawn maps missing/inactive child definitions to clean `STEP_FAILED` codes (`SPAWN_CHILD_DEFINITION_NOT_FOUND` / `SPAWN_CHILD_DEFINITION_INACTIVE`) instead of unhandled exceptions.
- Policy CEL is frozen into `frozen_steps` / `policy_definition` at saga start (parity with compensation); runtime gates evaluate the embed strictly — no `POLICIES_ROOT` fallback (migration `014_policy_definition.sql`).
- `output_schema` JSON is frozen onto `frozen_steps` as `output_schema_definition` at start/spawn hydrate; materialize and loop mint read the embed only (no runtime disk reload).
- Child spawn maps hydrate/asset freeze failures to `SPAWN_CHILD_HYDRATE_FAILED` and aborts remaining child creations.
- Startup schema sentinels now require `worker_definitions`, definition `body`/`is_active` columns, and `tools_bind` / `policy_definition` on step instances.
- Prompt Jinja rendering uses `SandboxedEnvironment` (file + inline paths), strips introspection helpers (`cycler` / `joiner` / `namespace` / `lipsum`), and fails closed on unsafe attribute access.

- ReAct `_submit` is accepted only as a singleton tool batch. Mixed batches run the other tools, feed a `_submit must be alone` tool error, and continue the loop (no silent short-circuit that drops sibling calls).
- Reason / simple human messages no longer `json.dumps` string prompts (Jinja and inline templates stay plain text). Structured dict/list prompt inputs are still JSON-encoded.
- Worker-scoped **skills** (`SKILLS_ROOT/<worker>/<id>.md`): step `skills.allow` drives a static `allowed_skills` index + virtual `load_skill`; skill frontmatter `allowed_tools` unions with `tools.allow` extras for react reason steps (`007_skills_allow.sql`).
- Coerce sloppy LLM tool arguments against MCP `inputSchema` before ReAct validation (common Ollama/vLLM stringified array/object fields)
- Admit LLM JSON against reason-step `output_schema` before validation (`_submit` and `simple` structured output)
- LLM JSON admission no longer crashes on nullable union `type` arrays; coerce string `"null"` / `"none"` to JSON `null` when the schema allows it
- `no_submit_call` no longer lists successful plain-text MCP tool output in `last_tool_errors`; expose final assistant text as `last_assistant_content` on `model_text_exit`

## [0.1.0] - 2026-06-30

### Added

- Open-core kernel: saga FSM, transactional outbox, CEL policy gates, compensation, HITL pause/resume, operator recovery
- `warden` CLI and engine HTTP API
- Worker runtime with LLM providers (`openai`, `local`, `mock`) and MCP tool integration
- Plugin registry (`WARDEN_PLUGINS`) with NoOp defaults; enterprise plugins ship from the separate **warden-enterprise** repository
- Docusaurus engineering manual (`docs/`, `website/`) with getting-started demos including credential-free mock LLM + MCP path
- `CONTRIBUTING.md`, `SECURITY.md`, and `CODE_OF_CONDUCT.md`

### Changed

- Documentation and website branding for public open-core launch

[Unreleased]: https://github.com/warden-runtime/warden-core/compare/v0.1.0...main
[0.1.0]: https://github.com/warden-runtime/warden-core/releases/tag/v0.1.0
