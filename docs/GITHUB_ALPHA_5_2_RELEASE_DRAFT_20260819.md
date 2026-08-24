# PeerBridge MCP v0.1.0-alpha.5.2

Alpha 5.2 turns PeerBridge into a local control room for observing and governing several AI
CLI sessions without weakening its evidence model. This is published as a normal GitHub
Release for discoverability, but the software itself remains Alpha and is not Stable.

## Highlights

- View PeerBridge room Agents, explicitly authorized desktop conversations, and managed or
  detected terminal/CLI sessions on one capability-explicit Agent Cockpit with Grid, Focus,
  Timeline, and stable Terminal, Activity, Answer, and Evidence views.
- Assign equal, researcher, implementer, reviewer, or custom work roles on the Conversation
  page, then use source-bound `View live work` navigation without copying or reconstructing
  context. Roles never add authority, tools, or voting weight.
- Keep observable output honest: task input is sent through stdin and not persisted by the
  Cockpit; hidden chain-of-thought and uncaptured external terminal history are never claimed.
- Use Implement + Review, Investigate + Debate, Read-only Audit, and Release Gate templates
  on a durable local queue with cancellation, timeout, retry classification, recovery, and
  opt-in scheduling. The guided room path binds exact active membership, roles, and complete
  route profiles, stops its exact discussion on source drift or timeout, and never retries a
  timed-out side-effectful run.
- Bind human permissions, isolated Git worktrees, Skill/MCP grants, typed decisions, task
  briefings, tests, reviews, completion, and Proof Bundles to exact source state. Deep
  Windows repositories use a short, repository-keyed LocalAppData worktree only when the
  project-local path would exceed Git's safe linked-worktree boundary.
- Detect stale evidence immediately and export a create-only, sanitized Proof Bundle. A
  separately installed trusted verifier rejects symbolic links, junctions, and other reparse
  points; unsigned bundles prove structural consistency only, not sender identity.
- Start bounded verification with the Control Room rather than the Trust page, and give a
  new operator a short read-only multi-Agent workflow that reuses an existing room.
- Reserve a versioned local event envelope for future encrypted continuity while keeping
  collaboration cloud transport disabled and SQLite authoritative in Alpha 5.2.
- Keep first-run guidance readable across Windows 100%, 125%, and 150% display scaling;
  transient windows follow the active Control Room across multi-monitor coordinates, and
  announcements wait until the tutorial closes instead of overlapping it.
- Use native WebView2 Modern Workbench as the default appearance and keep Pixel Control
  Room as the explicit `--legacy-pixel` compatibility surface; both preserve the same local
  authority and workflows.
- Display both package version and a path-free build digest, while keeping the update button
  honest about the difference between a published GitHub Release and an unpublished QA build.
- Use current, sanitized local receipts for Codex ACP (`gpt-5.6-sol` / high) and native
  Claude Code MCP (`claude-sonnet-5`), each invoking exactly one `bridge_status` tool without
  recording credentials. Grok 4.6 and Kimi relay routes pass the same tool gate. The installed
  official Grok ACP is authenticated and capability-discovered but its current free inference
  quota is exhausted; installed Kimi ACP requires operator login. These account states are
  visible and are not presented as product passes.
- Keep Edit and Full-development work bound to an approved governed worktree. Codex uses its
  reviewed workspace-write sandbox; Claude/Grok/Kimi use explicit provider-native policies,
  with Full access separately confirmed and never described as WSL isolation by default.
- Let manual clients issue or revoke an identity-bound capability through the CLI without
  exposing its secret contents.

## Windows portable

Download `PeerBridgeControlRoom-0.1.0a5.post2-windows-x64-portable.zip`, verify its entry in
`SHA256SUMS.txt`, extract the complete archive to a writable directory, and run
`Launch PeerBridge.cmd`.

The native executable is branded but not code-signed. Windows SmartScreen may therefore ask
you to review the unknown publisher. The portable archive does not contain provider
credentials or private runtime data and does not modify an existing Python environment.

## Verification material

The Release will include the Windows portable ZIP, wheel, source distribution, release notes,
`SHA256SUMS.txt`, provenance JSON, SBOM, and third-party license manifest. The portable ZIP
is produced from the annotated tag and accepted only after fresh extraction, native PE and
version checks, UI self-test, MCP send, create-only initialization, audit doctor, and launcher
lifecycle verification.

## Alpha boundaries

- Reviewed managed profiles include Codex read-only, Claude Code plan mode, and installed
  Kimi Code or Grok through the deny-all ACPX boundary. The official Grok Windows route is
  documented, but PeerBridge does not auto-run its mutable publisher installer script without
  a pre-bindable executable SHA.
- Cloud collaboration, accounts, billing, hosted execution, public remote shell access,
  automatic updates, code signing, and a native installer are not included.
- Existing announcement, private-feedback, and experimental tailnet features are separate
  data planes and are not collaboration sync.
- Linux remains core-only in this Alpha. A macOS Seatbelt/full-test matrix is configured on
  GitHub `macos-14`; a signed/notarized macOS app is not included.

See the [Alpha support matrix](alpha-support-matrix.md),
[Traditional Chinese quickstart](alpha-quickstart.zh-Hant.md), and
[Simplified Chinese quickstart](alpha-quickstart.zh-Hans.md) for operational details.
