# Roadmap

PeerBridge separates coordination from model execution. Roadmap items are intentions, not
claims about the current release.

The operator-requested desktop, provider, mobile, release, and business gaps are tracked in
[the 2026-08-15 desktop feature gap register](docs/DESKTOP_FEATURE_GAP_REGISTER_20260815.md).

## Public release train

PeerBridge uses a small number of substantial public releases. Internal acceptance stages do
not become public versions merely because one component passes.

### Alpha 5.2 - Best local Agent workspace

Target tag: `v0.1.0-alpha.5.2`; target package version: `0.1.0a5.post2`.

- [x] Complete one provider-neutral Cockpit for PeerBridge room, authorized desktop, and CLI
  conversations, including Conversation-page roles, source-bound navigation, truthful
  capability labels, mirror mode, and single-surface input control.
- [x] Add the bounded managed Codex and Claude Code session core with stable Terminal,
  Activity, Answer, and Evidence views.
- [x] Complete five-minute onboarding and connect existing rooms to workflow templates,
  durable local operations, scheduling, and isolated worktree execution without creating a
  second participant model.
- [x] Finish automatic, idempotent background verification triggers around the implemented
  Skill/MCP permissions, typed decisions, task briefing, Trust Timeline, stale-evidence
  detection, disagreement rechecks, Release Gate, and portable proof components.
- [x] Define a cloud-ready event envelope while keeping collaboration state local and the
  cloud collaboration capability disabled.

The complete normative scope and release gates are in
[Alpha 5.2 requirements](docs/ALPHA_5_2_REQUIREMENTS.md).

### Alpha 6 - Hardened execution platform

- [ ] Harden runtime adapters, worktree recovery, queue fairness, scheduling, permissions,
  upgrades, rollback, and compatibility based on Alpha 5.2 field evidence.
- [ ] Add reviewed managed launch profiles for further CLIs only after their non-interactive,
  permission, output, and process-lifecycle contracts are independently verified.
- [ ] Preserve local-first operation and the non-crippled open trust kernel.

### Beta 1 - Opt-in cloud continuity

- [ ] Add encrypted multi-machine rooms, remote Cockpit observation and approvals, device
  pairing, notifications, scoped remote MCP, and external audit anchors.
- [ ] Keep actual CLI execution on an authenticated desktop or runner; an Edge Worker is a
  coordination service, not a long-running Codex or Claude runtime.
- [ ] Keep collaboration services isolated from the existing announcement and private
  feedback infrastructure.

### 1.0 - Adaptive trust platform

- [ ] Stabilize APIs, schemas, migrations, receipt verification, and upgrade policy.
- [ ] Add evidence-derived capability routing, calibrated Agent selection, collaboration
  replay, and failure attribution only after sufficient real-world data exists.
- [ ] Publish an open evidence receipt protocol and complete external security review.

## Capability track: local coordination

- [x] Dependency-free stdio MCP server and shared SQLite store.
- [x] Arbitrary agent identities and expiring presence.
- [x] Path-scoped task leases, messages, reviews, proof, and audit chain.
- [x] Single-peer, presence-aware, solo, and configurable N-peer quorum policies.
- [x] Human control room and synthetic multi-agent demo.
- [x] Persistent global Agent identities and durable isolated multi-room coordination.
- [x] Provider-neutral Private/Room/Project memory with human-approved promotion.
- [x] Legacy MCP plus `2026-07-28` discovery compatibility.
- [x] Bounded Control Room history, active-tab rendering, and logical SQLite refresh.
- [x] Singleton mailbox supervisor with exact route matching and bounded runtime slots.
- [x] Per-room one-round fanout and bounded parallel discussion with consensus, stagnation,
  budget, and human-intervention controls.
- [x] Complete the repeatable memory/crash soak run and publish its evidence receipt.

## Capability track: authenticated remote transport

- [x] Add a loopback-only, single-tailnet human observation/message control plane.
- [x] Add scope isolation, CSRF, identity allowlist, rate limit, restart, and audit tests.
- [ ] Complete a real phone reconnect and mobile-browser E2E receipt before release.
  The real reconnect evidence is sealed; formal receipt finalization and strict release remain.
- [ ] Extract a storage interface and add PostgreSQL without changing coordination rules.
- [ ] Add authenticated Streamable HTTP MCP with per-agent identities and scoped tokens.
- [ ] Add a read-mostly web/PWA control room with explicit human approval actions.
- [ ] Add rate limits, tenant isolation, replay protection, and external audit anchors.
- [ ] Publish a migration and threat-model review before enabling remote writes.

## Capability track: optional runners and adapters

- [x] Define a provider-neutral OpenAI-compatible runner with bounded read-only MCP tools.
- [ ] Add opt-in adapters for MCP-native clients and API-backed model runners.
- [x] Add durable queues, cancellation, timeout, retry classification, and scheduling.
- [ ] Add provider-independent cost accounting without estimating unavailable usage.
- [ ] Add mobile notifications and approvals without exposing a desktop directly.

## Open-core commercialization

- [x] Define the non-crippled local open core and separable hosted-service boundary.
- [x] Add a default-off local aggregate analytics interface with no network sender.
- [x] Add a machine-readable capability manifest with dormant managed-service hooks.
- [ ] Add a transparent, self-hostable opt-in collector only after a separate privacy review.
- [ ] Periodically archive GitHub traffic and Release asset metrics without calling downloads users.
- [ ] Validate the boundary with users before implementing a paid control plane.
- [ ] Keep billing, hosted operations, enterprise governance, and managed sync outside
  the dependency-free local protocol and audit core.
- [ ] Verify any external open-source program terms from official sources before making
  an eligibility or benefit claim.

See [open-core and commercial boundary](docs/open-core-boundary.md).

## Explicit boundaries

- An MCP server coordinates tools; it does not keep a model alive by itself.
- A cloud deployment needs separate authenticated agent runners to perform work.
- No public unauthenticated listener will be shipped.
- The zero-rental Tailscale page is a narrow human control plane, not remote MCP.
- Provider credentials stay outside messages, audit payloads, and the project database.
- Grok, DeepSeek, Kimi, Codex, and Claude are integration targets, not bundled dependencies.
