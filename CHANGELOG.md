# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `provider: azure` — Azure OpenAI / Microsoft Foundry via LangChain `ChatOpenAI` and the OpenAI-compatible `/openai/v1/` path (`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`; worker `model_name` is the deployment name). Defaults to Chat Completions for prompt-cache friendliness; Responses API is opt-in via `WARDEN_AZURE_USE_RESPONSES_API`.
- Submit-mode ReAct soft-feeds **recoverable** tool mismatches (e.g. `search_replace` `old_text not found` / non-unique match, missing path, patch-apply text failures) into the transcript with a one-line recovery hint instead of failing the step with `TOOL_OUTPUT_ERROR`. `apply_patch_sandbox` JSON rejects get the same hint. Transport/`MCP error` / invalid-argument failures remain hard. Override via `ToolLifecycleHooks.tool_output_is_recoverable`.

### Fixed

- ReAct `_submit` is accepted only as a singleton tool batch. Mixed batches run the other tools, feed a `_submit must be alone` tool error, and continue the loop (no silent short-circuit that drops sibling calls).
- Reason / simple / compensation human messages no longer `json.dumps` string prompts (Jinja and inline templates stay plain text). Structured dict/list prompt inputs are still JSON-encoded.
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
