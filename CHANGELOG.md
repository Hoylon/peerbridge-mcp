# Changelog

All notable changes are documented here. The format follows Keep a Changelog and the
project uses Semantic Versioning after the initial alpha series. During the Alpha series,
major milestones use names such as Alpha 5, while compatible maintenance releases use
Alpha 5.1, Alpha 5.2, and so on.

## [Unreleased]

## [0.1.0-alpha.5.2] - 2026-08-23

### Added

- A native WebView2 Modern Workbench desktop that maps all twelve Control Room pages; the
  Pixel Control Room remains available through the explicit `--legacy-pixel` option.
- Metadata-first, user-selected history import for Codex, Claude Code, Grok Build, and Kimi
  Code. No conversation is selected by default; imported rooms are create-only,
  source-SHA-bound, secret-redacted, paged, and read-only.
- Governed Edit and Full-development launch tiers for official persistent runtimes. A
  write-capable launch requires the exact active human-approved isolated Git worktree and
  never targets the operator checkout.
- An in-app Agent Cockpit with Grid, Focus, Timeline, and stable per-session Terminal,
  Activity, Answer, and Evidence views for concurrent PeerBridge-room, authorized-desktop,
  and terminal/CLI work through one capability-explicit session contract.
- Persistent official launch profiles for Codex app-server, Claude Code stream-json, and
  installed Grok/Kimi ACPX runtimes, with bounded redacted capture and owned start, send,
  interrupt, stop, resume, and process-tree lifecycle controls.
- Four local workflow templates backed by a durable operation queue with cancellation,
  timeout, retry classification, crash reconciliation, and opt-in interval scheduling.
- Conversation-page work roles, source-bound `View live work` navigation, and a bounded
  first-workflow path that reuses the selected room instead of creating another participant
  model.
- Human-approved isolated Git worktrees, versioned Skill/MCP capability grants, typed
  decision memory, task briefings, conflict findings, and exact source-state verification.
- A Trust Timeline with stale-evidence detection, bounded disagreement rechecks, and
  create-only portable Proof Bundles requiring a trusted installed verifier; unsigned
  bundles are explicitly structural-only and never carry executable verification code.
- A versioned metadata-only local event envelope reserved for future encrypted continuity;
  collaboration transport remains disabled.
- Privacy-safe illustrated guides for all twelve Control Room panels, opened directly on
  the current panel with three numbered operating steps.
- Provider-native governed write profiles for Claude, Grok, and Kimi, with standard Edit
  separated from explicitly confirmed Full access; optional WSL2 and macOS policies remain
  defense-in-depth contracts rather than claimed default isolation.
- `peerbridge identity issue/revoke` so manual MCP clients can provision a project-, scope-,
  and Agent-bound capability without printing its secret contents.

### Changed

- Expanded the Control Room to twelve localized pages and kept all navigation available at
  the supported minimum window size through a permanently visible scrollbar, direction
  buttons, and an explicit scroll prompt.
- Added Today, Last 7 days, Last 30 days, and All time API usage views with hourly, daily,
  and monthly buckets applied consistently to summaries, charts, provider/model breakdowns,
  and tables.
- Distinguished configured model routes from locally installed Agent terminals and named
  install, update, official-guide, and documentation actions by their actual target.
- Advanced the append-only SQLite authority to schema 25 for operations, schedules,
  execution bindings, capabilities, grants, typed decisions, briefings, trust records, and
  bounded authorized desktop/terminal session adapters.
- Start bounded, idempotent verification triggers with the Control Room so due schedules,
  stale bindings, permission-sensitive work, failed operations, and explicit release
  requests are materialized without requiring the Trust page to be opened.
- Made the reviewed first-run path start in Agent Cockpit while keeping provider setup,
  rooms, and trust workflows in one local authority.

### Fixed

- Bind one-click npm Agent/runtime installation to the exact downloaded tarball bytes:
  PeerBridge stages the pinned version, computes and compares its SHA-512 before launching
  npm against that local archive, revalidates file identity immediately before launch, and
  disables lifecycle scripts unless the reviewed package explicitly requires one.
- Isolate Agent Cockpit Grid, Focus, selector, and Timeline content by the active room while
  retaining explicitly unbound observable sessions, and clear in-memory output if an adapter
  violates immutable source/session/room binding instead of relabelling stale conversation data.
- Keep isolated Git worktrees project-local when path-safe, with a repository-keyed
  LocalAppData fallback for deep Windows paths; verify that either location remains linked
  to the exact bound Git common directory.
- Bind the first guided workflow to its exact active membership, role, complete route
  profile, prompt, and room discussion instead of treating a successful launch as
  completion; stop that exact discussion on cancellation, source drift, or timeout, and do
  not replay a timed-out side-effectful run.
- Treat an absent Windows singleton mutex as no existing Control Room, allowing a fresh
  native or portable launch to initialize its workspace instead of exiting before startup.
- Add schema 18 memory columns before creating their dependent index so existing Alpha 5.1
  databases migrate additively without losing prior memory rows.
- Wrap the Cockpit observable-output notice and retain localized ready state without
  replacing live running or error status.
- Center first-run tutorial and announcement windows over the actual Control Room position,
  including negative-coordinate displays, and defer announcements until the tutorial closes.
- Keep the reviewed Control Room layout usable at Windows 100%, 125%, and 150% display
  scaling without hiding the sidebar navigation or chat composer.
- Show the complete package version on its own sidebar line instead of clipping maintenance
  suffixes such as `post2` after the localized status text.
- Show a path-free executable build digest so same-version QA builds are distinguishable,
  and report “no newer published GitHub Release” without calling an unpublished local build
  the latest release.
- Preserve explicitly exposed identity-bound collaboration tools while keeping the default
  MCP surface read-only and permanently denying unrestricted artifact content reads.
- Normalize Claude's official environment family across native launch paths, keep persistent
  two-turn sessions active, and surface Codex provider quota failures without retaining the
  provider's private message.
- Compare semantic image answers as UTF-8 bytes so non-ASCII model output fails cleanly
  instead of crashing the verification thread.
- Keep the mobile chat composer and send button inside the viewport by binding every chat
  grid row explicitly, and reset the workspace scroll position when switching pages.
- Count one current TTL-live presence per Agent instead of presenting historical sessions as
  connected Agents.

### Security

- Never persist Cockpit task input, never present hidden chain-of-thought as visible, and
  redact credential-shaped output before display or retention.
- Keep Proof Bundles create-only, source-bound, path-sanitized, credential-free, and
  independently verifiable; reject symbolic links, Windows junctions, and other reparse
  points before reading bundle files.
- Keep local SQLite authoritative and separate future collaboration data from announcement
  and feedback infrastructure.
- Bind every writer to an exact human-approved Git worktree, distinguish that governance
  boundary from OS sandboxing, and fail closed on unsupported permission escalation.

## [0.1.0-alpha.5.1] - 2026-08-18

### Fixed

- Allow immediate POSIX loopback restart after prior client sockets enter `TIME_WAIT`,
  while preserving exclusive listener ownership on Windows.
- Keep the chat attachment picker owned by the control-room window so it cannot open
  invisibly behind the application.
- Ship and launch the versioned native Windows executable with the embedded PeerBridge
  icon for the window, taskbar, desktop shortcut, and Start menu shortcut.
- Focus an already-running control room instead of starting a competing source or frozen
  instance.
- Recognize Alpha maintenance versions and GitHub's display-case repository URLs in the
  update checker and release acceptance workflow.

### Changed

- Isolate packaged lifecycle verification with a bounded verifier-only instance identity,
  while keeping the production single-instance mutex unchanged.

## [0.1.0-alpha.5] - 2026-08-18

### Added

- A package-pinned public HTTPS announcement feed with Traditional Chinese, Simplified
  Chinese, and English delivery in the control room.
- Published-release acceptance on a fresh GitHub-hosted Windows VM, including exact asset
  SHA-256 verification, frozen lifecycle checks, and a live announcement-feed receipt.

### Changed

- Reframed the public introduction around official clients, relay services, compatible
  APIs, local models, equal-peer collaboration, mutual scoring, cross-agent audit,
  approved shared memory, live Token visibility, and one-click CC Switch model sync.

### Fixed

- Included the maintainer-pinned announcement configuration in the Windows/Python package
  so clean installations can receive the public feed without local setup.

## [0.1.0-alpha.4] - 2026-08-18

### Changed

- Published under the Hoylon maintainer identity and moved all trusted repository,
  update, security, packaging, and release links to `github.com/hoylon/peerbridge-mcp`.

## [0.1.0-alpha.3] - 2026-08-18

### Added

- Provider-reported input, output, cached-input, and reasoning-token accounting with a
  local-only usage dashboard, four-series trend chart, provider/model horizontal bars,
  and explicit partial/unavailable coverage instead of text-length estimates.
- Provider-independent private feedback through an authenticated HTTPS edge, private R2
  bundles, D1 metadata, fixed-maintainer notifications, bounded retry, retention, and
  exact bundle re-download verification.
- Complete Traditional Chinese, Simplified Chinese, and English normal-use UI catalogs.

### Changed

- Hardened provider child lifecycle and crash recovery with bounded descendant cleanup,
  stale-lease reconciliation, and resource guards.
- Hardened MCP request replay and room dispatch recovery without reply fanout loops.
- Refined the Windows control-room layout, refresh behavior, usage charts, feedback flow,
  and native bridge branding for the portable build.

### Security

- Tightened feedback ZIP validation, distributed rate limits, retention cleanup,
  notification retries, credential redaction, and release-source secret scanning.

## [0.1.0-alpha.2] - 2026-08-16

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

[Unreleased]: https://github.com/hoylon/peerbridge-mcp/compare/v0.1.0-alpha.5.2...HEAD
[0.1.0-alpha.5.2]: https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0-alpha.5.2
[0.1.0-alpha.5.1]: https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0-alpha.5.1
[0.1.0-alpha.5]: https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0-alpha.5
[0.1.0-alpha.4]: https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0-alpha.4
[0.1.0-alpha.3]: https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0-alpha.3
[0.1.0-alpha.2]: https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0-alpha.2
[0.1.0-alpha.0]: https://github.com/hoylon/peerbridge-mcp/releases/tag/v0.1.0-alpha.0
