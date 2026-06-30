# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Open-core kernel: saga FSM, transactional outbox, CEL policy gates, compensation, HITL pause/resume, operator recovery
- `warden` CLI and engine HTTP API
- Worker runtime with LLM providers (`openai`, `local`, `mock`) and MCP tool integration
- Plugin registry (`WARDEN_PLUGINS`) with NoOp defaults; enterprise plugins ship from the separate **warden-enterprise** repository
- Docusaurus engineering manual (`docs/`, `website/`) with getting-started demos including credential-free mock LLM + MCP path
- `CONTRIBUTING.md`, `SECURITY.md`, and `CODE_OF_CONDUCT.md`

### Changed

- Documentation and website branding for public open-core launch

## [0.1.0] - 2026-06-30

Initial public release of the Warden open-core kernel.

[Unreleased]: https://github.com/warden-runtime/warden-core/compare/v0.1.0...master
[0.1.0]: https://github.com/warden-runtime/warden-core/releases/tag/v0.1.0
