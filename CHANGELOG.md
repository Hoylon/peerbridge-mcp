# Changelog

All notable changes are documented here. The format follows Keep a Changelog and the
project uses Semantic Versioning after the initial alpha series.

## [Unreleased]

## [0.1.0-alpha.1] - 2026-08-16

### Added

- First-class direct OpenAI-compatible Grok and Kimi routes backed by Windows
  Credential Manager, without requiring CC Switch.
- Separate outbound model and expected response-model alias bindings with fail-closed
  completion identity checks.
- Canonical model-registry hashes that ignore provider ordering, duplicates, and
  unrelated response metadata.
- Runtime identity labels for MCP client, provider route, and selected model.
- Auditable Agent/provider/model/reasoning route requests and saved route profiles.
- Fail-closed runtime matching and SHA-bound route receipts on acknowledgement.
- Route controls and `REQUESTED`/`VERIFIED` visibility in the pixel monitor.
- Per-server MCP tool allowlists for least-privilege provider probes.
- Safe provider registry with Windows Credential Manager-backed secrets and redacted
  MCP/SQLite metadata.
- CC Switch public-CLI discovery, model listing, route registration, and explicit switch.
- Seven-page control room with Agent -> provider -> model -> reasoning routing cascade
  and a dedicated Shared Memory inspector.
- Persistent global Agent catalog and reusable room seats so one Agent can participate in
  multiple isolated conversations without disappearing from the home library.
- Durable rooms with independent membership sessions, cursors, cross-room reply rejection,
  and backward-compatible room-bound collaboration receipt v2 verification.
- Provider-neutral Private/Room/Project Memory Ledger with explicit human promotion,
  source SHA bindings, append-only revocation, and room/owner authorization checks.
- Read-only memory tools in the OpenAI-compatible provider runner, with receipts that omit
  prompts, memory bodies, endpoints, and credential contents.
- Loopback-only Tailscale Serve control page for private scope-bound mobile observation
  and audited human MCP messages.
- Tailnet identity allowlist, SHA-derived operator IDs, CSRF/same-origin enforcement,
  write rate limiting, socket limits, and same-port restart coverage.
- Per-room `off`, one-round fanout, and bounded parallel discussion policies.
- Deterministic consensus, blocker, stagnation, round-budget, message-budget, and human
  pause/resume/continue/stop controls for automated room discussions.
- Bounded parallel supervisor dispatch for complete room rounds, with one authorized
  coordinator transaction advancing each completed round.
- Content-addressed desktop chat attachments with type, count, size, credential-pattern,
  project-boundary, and release-package safeguards.
- Traditional Chinese, Simplified Chinese, and English UI foundation with persisted locale.
- Replayable first-run tutorial and explicit read-only release update checker.
- Provider-independent private feedback flow with encrypted credential escalation and a
  local case-bundle fallback.
- Default-off, local-only aggregate usage analytics and documented metric limitations.
- Bounded resource guard and 1,200-message local Alpha memory soak receipt.
- Create-only Windows x64 portable ZIP packaging with an owner-authored application icon,
  fresh extraction verification, UI self-test, frozen MCP send test, initialization, and doctor.

### Fixed

- Keep the desktop control room visually continuous by skipping unchanged widget redraws,
  appending new chat bubbles incrementally, preserving the reader's scroll position, and
  showing a quiet localized refresh timestamp instead of flashing the entire view.
- Label the locale selector with the permanent English word `Language` so users can find
  the language control before they understand the active UI locale.
- Apply protected/sensitive path checks to hash-only artifact reads.
- Enforce MCP tool input schemas at runtime before writing tool-call audit events.
- Preserve recoverable modern MCP tool errors while rejecting unsupported fields as
  JSON-RPC invalid parameters.
- Verify legacy, room-bound, and discussion-bound message SHA contracts without
  rewriting historical messages.
- Route frozen desktop child CLI invocations back into the PeerBridge CLI so packaged
  composer sends do not reopen the monitor executable.

## [0.1.0-alpha.0] - 2026-08-11

### Added

- Dependency-free local stdio MCP server backed by SQLite WAL.
- Per-consumer mailbox cursors and SHA-bound receipts.
- Expiring task leases with path conflict detection.
- Presence-aware, solo, and required-peer approval policies.
- Peer review requests, proof records, live file rehash, and completion gate.
- Append-only per-scope event hash chain and verifier.
- Human-operable pixel control room.
- Codex and Claude Code configuration examples.
- Legacy and MCP `2026-07-28` dual-era transport support.
- Arbitrary agent identities, configurable N-peer review quorum, and multi-agent monitor tiles.
- Additive schema v1/v2-to-v4 migration and subprocess stdio interoperability tests.

[Unreleased]: https://github.com/oscarho200407-hue/peerbridge-mcp/compare/v0.1.0-alpha.1...HEAD
[0.1.0-alpha.1]: https://github.com/oscarho200407-hue/peerbridge-mcp/compare/v0.1.0-alpha.0...v0.1.0-alpha.1
[0.1.0-alpha.0]: https://github.com/oscarho200407-hue/peerbridge-mcp/releases/tag/v0.1.0-alpha.0
