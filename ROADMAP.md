# Roadmap

PeerBridge separates coordination from model execution. Roadmap items are intentions, not
claims about the current release.

## v0.1 - Local coordination

- [x] Dependency-free stdio MCP server and shared SQLite store.
- [x] Arbitrary agent identities and expiring presence.
- [x] Path-scoped task leases, messages, reviews, proof, and audit chain.
- [x] Single-peer, presence-aware, solo, and configurable N-peer quorum policies.
- [x] Human control room and synthetic multi-agent demo.
- [x] Legacy MCP plus `2026-07-28` discovery compatibility.

## v0.2 - Authenticated remote transport

- [ ] Extract a storage interface and add PostgreSQL without changing coordination rules.
- [ ] Add authenticated Streamable HTTP MCP with per-agent identities and scoped tokens.
- [ ] Add a read-mostly web/PWA control room with explicit human approval actions.
- [ ] Add rate limits, tenant isolation, replay protection, and external audit anchors.
- [ ] Publish a migration and threat-model review before enabling remote writes.

## v0.3 - Optional runners and adapters

- [ ] Define a provider-neutral runner protocol with bounded workspaces and budgets.
- [ ] Add opt-in adapters for MCP-native clients and API-backed model runners.
- [ ] Add durable queues, cancellation, timeout, retry, and cost accounting.
- [ ] Add mobile notifications and approvals without exposing a desktop directly.

## Explicit boundaries

- An MCP server coordinates tools; it does not keep a model alive by itself.
- A cloud deployment needs separate authenticated agent runners to perform work.
- No public unauthenticated listener will be shipped.
- Provider credentials stay outside messages, audit payloads, and the project database.
- Grok, DeepSeek, Kimi, Codex, and Claude are integration targets, not bundled dependencies.
