# Security policy

## Supported versions

Warden is in early development (`0.1.x`). Security fixes land on the default branch (`main`) and are included in the next tagged release when one is cut.

| Version | Supported |
|---------|-----------|
| `0.1.x`   | Yes       |
| `< 0.1`   | No        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report them privately so we can assess and patch before details are public:

1. Email **[security@warden-runtime.org](mailto:security@warden-runtime.org)**, **or**
2. Open a **[GitHub private security advisory](https://github.com/warden-runtime/warden-core/security/advisories/new)** against this repository.

Include:

- A description of the issue and likely impact
- Steps to reproduce (proof of concept if you have one)
- Affected components (engine API, worker, CLI, manifests, Compose stack, etc.)
- Warden version or commit SHA

We aim to acknowledge reports within **5 business days** and will keep you updated on remediation timing.

## Scope notes

These are **in scope** for this policy:

- Remote code execution or privilege escalation in Warden services
- Authentication or authorization bypass in shipped HTTP/CLI surfaces
- SQL injection or unsafe deserialization in engine/worker paths
- Cross-tenant or cross-saga data leakage through the API or worker loop
- Supply-chain issues in this repository's release artifacts

These are generally **out of scope** (document and harden in your deployment instead):

- Misconfigured `.env`, exposed Postgres, or Docker socket mounts in operator Compose files
- Compromised LLM provider keys or MCP server credentials you supply
- Denial-of-service from unauthenticated load against a publicly exposed engine with no rate limits
- Issues in third-party MCP servers or model providers outside this repo

## Safe deployment

Warden is designed to run **inside your infrastructure**. For production:

- Do not expose the engine API to the public internet without authentication and network controls
- Treat MCP servers and LLM endpoints as trusted only to the degree you configure them
- Rotate secrets; keep Postgres and Jaeger off public routes
- Review [`docs/getting-started/configuration.md`](docs/getting-started/configuration.md) and [`docs/getting-started/troubleshooting.md`](docs/getting-started/troubleshooting.md) for operator guidance

## Recognition

We credit reporters in release notes when fixes ship, unless you prefer to remain anonymous.
