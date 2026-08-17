# PeerBridge MCP

[English](README.md) | [繁體中文](README.zh-Hant.md) | [简体中文](README.zh-Hans.md)

<p align="center">
  <img src="src/peerbridge_mcp/release_support/peerbridge-icon.png" width="128" alt="PeerBridge bridge logo">
</p>

Bring Codex, Claude Code, Grok, Kimi, DeepSeek, Gemini, local models, and other MCP or
OpenAI-compatible Agents into one auditable AI team.

PeerBridge connects official clients, relay services, compatible APIs, and local models
without locking the team to one provider. It gives every Agent an equal seat, shared
approved memory, bounded parallel discussion, task ownership, mutual scoring,
cross-agent audits, and a human-controlled room where work remains visible. A live Token
dashboard shows usage by provider and model. PeerBridge runs locally on SQLite, keeps
provider credentials out of chat and project history, and preserves a SHA-linked record
of messages, decisions, evidence, scores, reviews, and handoffs.

Human or Agent messages can wake the room, collaboration stops on consensus, blockers,
stagnation, or explicit limits, and the operator can intervene at any time.

> Status: alpha. The coordination and audit core is tested, but the public API and
> database schema may change before 1.0.

Operational continuity and commercialization are explicit design boundaries. See
[memory and long-running operations](docs/operations-memory.md) and the
[open-core boundary](docs/open-core-boundary.md). The remote/mobile module is
**Experimental and default-off**. Roadmap entries are not capability claims.

## Why

Running two coding agents against one repository creates predictable failure modes:

- both agents edit the same files;
- one agent assumes a peer is online when it is not;
- chat messages are lost or acknowledged globally instead of per consumer;
- a review approves stale files rather than the files that were actually tested;
- the human cannot see who owns a task or why it was marked complete.

PeerBridge addresses those coordination failures without making either agent the boss.
One task has one writer lease, peers can review each other, and a human can intervene.

## Architecture

```mermaid
flowchart LR
    H["Human operator"] --> M["Pixel control room"]
    C["Codex / Claude Code"] --> S1["PeerBridge stdio process"]
    A["Grok / Kimi / DeepSeek / Gemini / local Agents"] --> S2["PeerBridge stdio process"]
    M --> S3["PeerBridge stdio process"]
    R["Private mobile UI"] -->|"Tailnet HTTPS"| T["Tailscale Serve"]
    T -->|"loopback only"| S4["Human MCP message gateway"]
    S1 --> DB[("Shared SQLite store")]
    S2 --> DB
    S3 --> DB
    S4 --> DB
    DB --> L["SHA-linked audit events"]
    DB --> W["Mailbox, leases, reviews, proofs"]
```

Each MCP client launches its own stdio server process with a distinct `--agent-id`.
Those processes coordinate through one project-local `.peerbridge/peerbridge.sqlite3`
database. SQLite WAL mode and `BEGIN IMMEDIATE` transactions serialize state changes.

## Features

- Per-agent presence with expiry, not a permanent online flag.
- Audited runtime identity labels for client, provider route, and selected model.
- SHA-bound direct and broadcast messages.
- Persistent global Agent Library with reusable, room-scoped seats; adding an Agent to one
  room never removes it from the library or another room.
- Durable multi-room conversations with independent membership sessions, inbox cursors,
  reply boundaries, and room-bound collaboration receipts.
- Provider-neutral Memory Ledger with owner-only Private, membership-bound Room, and
  human-approved Project records, each SHA-bound to its explicit source evidence.
- Per-consumer receipts and contiguous durable cursors.
- Capability-token task leases with expiry and recovery.
- Deterministic read/write path overlap checks.
- `solo_allowed`, `two_party_required`, `presence_aware`, and N-peer quorum policies.
- Source-bound peer review requests and equal-peer verdicts.
- Mutual Agent scoring and cross-agent audit trails linked to the exact reviewed source.
- Live Token usage dashboard with provider/model breakdown and input, output, cache-write,
  and cache-read trends.
- One-click CC Switch provider/model synchronization through its official CLI, while
  credentials stay in the user's existing CC Switch installation.
- Live file rehash before task completion.
- Isolated plan and patch drafts that are never applied automatically.
- Append-only per-scope SHA-256 event chain with a verifier.
- Pixel-style local control room with human MCP message composition.
- The coordination core has no runtime dependencies beyond Python's standard library;
  optional encrypted feedback uses the `feedback` extra (`cryptography`).
- Dual-era MCP support: legacy initialization and the `2026-07-28` discovery model.
- Optional zero-rental private mobile control through loopback plus Tailscale Serve.
- Bounded room-history paging, active-tab rendering, and a singleton low-memory mailbox
  supervisor for optional provider runners.

## Quickstart

### Windows portable app

Download `PeerBridgeControlRoom-0.1.0a5-windows-x64-portable.zip` from the GitHub
Alpha release, extract the complete ZIP to a writable folder, and double-click
`Launch PeerBridge.cmd`. The portable app creates its local workspace under
`%LOCALAPPDATA%\PeerBridge\workspace`; it does not include provider credentials or
private runtime data.

This Alpha executable is not code-signed, so Windows SmartScreen may ask you to review
the publisher. Verify the release SHA-256 before opening it. PeerBridge does not provide
an auto-installer or modify an existing Python environment.

### Install from source

Requirements: Python 3.11 or newer.

```powershell
git clone https://github.com/hoylon/peerbridge-mcp.git
cd peerbridge-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
peerbridge init --project-root . --scope demo
peerbridge doctor --project-root . --scope demo
```

Run a server manually:

```powershell
peerbridge serve --project-root . --agent-id codex-main --scope demo
```

When one client can select several official or relay-backed models, record the route
without exposing credentials:

```powershell
peerbridge serve --project-root . --agent-id grok-relay-reviewer --scope demo `
  --client-name relay-coding-client `
  --provider-id relay:grok-official-channel `
  --model-id grok
```

`agent-id` identifies the logical worker. `client-name` identifies the MCP-capable
application or adapter. `provider-id` is an operator-supplied non-secret route label,
and `model-id` identifies the selected model. A Grok or DeepSeek official website and a
relay route are separate identities even when they expose the same model family.

Direct OpenAI-compatible endpoints do not require CC Switch. On the **Safe
connections** page, save the API base URL and API key into Windows Credential Manager,
then register one route per logical Agent. The API key never enters SQLite or an MCP
message. Some relays accept one requested model ID but report a stable deployment
alias in responses. In that case set **EXPECTED RESPONSE MODEL** explicitly. For
example, a route may request `grok-4.6` while requiring the response to report
`grok-4.6-build`. An unconfigured or changing alias fails closed rather than being
silently treated as the requested model.

The server speaks newline-delimited JSON-RPC over stdin/stdout. Usually an MCP client
starts it for you, so a manually started server waiting for input is expected behavior.

## Connect clients

Use an absolute Python executable path so each client launches the same installed
environment and shared database.

### Codex

```powershell
codex mcp add peerbridge -- C:\path\to\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve --project-root C:\path\to\your-project `
  --agent-id codex-main --scope your-project
```

### Claude Code

```powershell
claude mcp add --scope project --transport stdio peerbridge -- `
  C:\path\to\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve --project-root C:\path\to\your-project `
  --agent-id claude-code --scope your-project
```

Use different MCP server entry names when both clients write a shared project config,
for example `peerbridge-codex` and `peerbridge-claude`. See
[client configuration](docs/client-config.md) for TOML, JSON, Linux, and macOS examples.
The same stdio server can be registered in Kimi Code CLI; other providers may require a
separate API-backed runner. See [agent integration boundaries](docs/agent-adapters.md).

PeerBridge reports the effective capability of each bound room Seat instead of inferring
it from the Agent name:

- `MCP NATIVE`: a real MCP-capable client or terminal owns the session and can call the
  PeerBridge server directly.
- `MCP TOOL`: an API-backed model is inside PeerBridge's bounded, allowlisted MCP tool
  loop; this is tool-capable but is not represented as a native client session.
- `INFERENCE`: the route can return a bounded model answer but cannot call PeerBridge
  tools. Web chat tabs and one-shot CLI fallbacks belong here.

Any other client or terminal that implements MCP can use the same stdio command shown in
[client configuration](docs/client-config.md). A configured route is not considered live
until that client is actually running and authorized.

Official client references:

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Kimi Code CLI](https://github.com/MoonshotAI/kimi-cli)

## Pixel control room

```powershell
peerbridge monitor --project-root C:\path\to\your-project --scope your-project
```

The monitor displays messages, task ownership, peer reviews, proof records, and the
event log. Its composer sends a human message through the same MCP stdio path as an
agent, so human intervention receives the same hashes and audit trail.

Agents live in a persistent **Global Agent Library**. Dragging or adding an Agent to a
room creates a room-scoped seat; it does not move or consume the global identity. The
same Agent can therefore participate in several rooms at once. Every seat has an
independent `room_session_id`, requested route, and room cursor. Removing a seat stops
future delivery in that room while retaining its messages and audit history. Context is
not silently copied between rooms; an explicit, audited summary message is required when
the operator wants to share context.

Each custom room has an explicit automation policy. **Off** records history without
waking a model. **One round** asks every routed Agent seat in parallel exactly once.
**Bounded discussion** advances parallel rounds only after all current dispatches are
terminal. Replies end with `CONTINUE`, `CONSENSUS`, or `BLOCKED`; the coordinator stops on
consensus, blockers, stagnation, the round limit, or the message budget. The room bar lets
the human pause and resume an in-flight round, continue a bounded discussion after an
automatic stop condition, or stop it explicitly.

Replies never create an uncontrolled reply cascade. Only coordinator-created prompts are
dispatchable, round advancement is idempotent, and each scope has one supervisor writer
lock. Starting a new room post stops an older open discussion while retaining its history
and stop reason.

The desktop composer can attach up to five explicitly selected PNG/JPEG/GIF/WebP or
UTF-8 text/Markdown/CSV/JSON/log files (8 MiB each, 16 MiB total). PeerBridge validates
the declared type, rejects credential-like text, copies each file into the ignored local
`.peerbridge-artifacts/chat/` store under its SHA-256 name, and binds only that relative
content-addressed path to the message. The original absolute path and filename do not
enter SQLite. Initial fanout/discussion prompts receive the binding once; later rounds do
not duplicate it. Transporting an attachment does not prove that a selected provider can
interpret images, so unsupported routes must treat it as an auditable file reference.

The **Shared Memory** page shows explicit memory records separately from chat. PeerBridge
does not extract or synchronize hidden chain-of-thought, provider-side conversation state,
or credentials. An Agent may write its own Private scratch summary, active room members may
read Room memory, and only `human-operator` may publish or revoke Project memory. Project
promotion requires a SHA-bound source message, parent memory, or project artifact. Revocation
preserves the original record and appends a new audit receipt instead of deleting history.

API-backed OpenAI-compatible runners receive only the read-only `list_memories` and
`read_memory` tools by default. They can therefore use the same approved facts as Codex,
Claude Code, Grok, Kimi, DeepSeek, or a local model without gaining permission to publish
project-wide memory. Every room keeps an independent model session and cursor even when the
same global Agent has a seat in several rooms.

## Private mobile control

The optional **Experimental** remote page exposes a deliberately narrow human interface through
Tailscale Serve. The backend binds only to `127.0.0.1`; the tailnet proxy supplies the
authenticated user identity. It supports scope-bound observation and audited human MCP
messages, not shell execution or file mutation.

```powershell
.\scripts\launch_remote_control.cmd -Port 8765 -Scope your-project
```

See [private remote and mobile control](docs/remote-mobile.md) for the security boundary,
tests, phone setup, and the explicit prohibition on public Tailscale Funnel exposure.

## Product status and opt-in metrics

PeerBridge exposes a machine-readable capability boundary without activating a hosted
service, payment flow, or commercial entitlement provider:

```powershell
peerbridge product --project-root . status
peerbridge product --project-root . status --capability remote.experimental.self_hosted
```

The local analytics hook is **OFF by default**. Without explicit opt-in it does not create
an installation ID, queue an event, or contact a network endpoint. The current Alpha has
no analytics sender at all; enabled data remains on the local machine as UTC-day aggregate
counters with a random, resettable installation ID.

```powershell
peerbridge analytics --project-root . status
peerbridge analytics --project-root . enable
peerbridge analytics --project-root . export
peerbridge analytics --project-root . disable
```

GitHub Release asset `download_count` measures file downloads, not unique users. Actual
DAU/WAU/MAU estimates require a future transparent collector plus explicit app opt-in and
must be described as active **installations**, not people. Prompts, message bodies, API
keys, model outputs, file names/paths, project names, account identities, IP addresses and
arbitrary metadata are outside the public event schema. See
[telemetry and launch metrics](docs/telemetry.md) and
[Experimental remote/commercial hooks](docs/experimental-remote-commercial-hooks.md).

Announcements are independent of analytics. The packaged Alpha enables a read-only HTTPS
announcement connection by default so urgent notices can appear without an update check.
Each request sends the selected UI locale, the per-locale announcement cursor, and a fixed
non-identifying PeerBridge announcement client User-Agent; normal network infrastructure
can also observe the source IP address. It sends no
installation ID, credentials, project paths, message content, or model output. The
Announcements page provides a separate network switch: turning it off stops announcement
requests while leaving the bounded local cache readable. Popup notifications have their
own independent preference. If the saved preference file is unreadable, both network
polling and popups fail closed until the user explicitly saves new preferences.

Feedback is submitted privately over HTTPS. Ordinary report metadata, user-selected
diagnostics, contact details, and attachments are protected in transit but are not
end-to-end encrypted inside the support bundle. Only an optional credential that the user
explicitly chooses to include is encrypted locally to the pinned maintainer support public
key before upload. See [feedback privacy](docs/feedback-privacy.md).

The control room provides a persisted `zh-Hant` / `zh-Hans` / English locale foundation,
a replayable first-run tutorial, and an explicit read-only update check. An Alpha install
reports newer GitHub releases. A future Stable channel will only follow the Stable release
track.
The checker never downloads or installs code; signed one-click update and rollback remain
future work.

The **Safe connections** page supports two local onboarding paths:

- enter a private HTTPS endpoint and API key; PeerBridge stores both in the current
  Windows user's Credential Manager and writes only redacted identifiers and SHA-256
  fingerprints through MCP;
- discover existing Claude, Codex, Gemini, OpenCode, Hermes, or OpenClaw providers
  through the official CC Switch CLI,
  fetch model IDs using CC Switch's already-saved credential, register a PeerBridge
  route, and switch only after an explicit human confirmation.

For a host-only OpenAI-compatible URL, PeerBridge adds the conventional `/v1` API
base. If the provider publishes an explicit compatibility path, enter that complete
base path; PeerBridge preserves it. This supports endpoints such as Gemini's
`/v1beta/openai/` compatibility base without provider-specific code.

PeerBridge does **not** put raw API keys or full private endpoints in MCP messages,
SQLite, audit events, receipts, logs, command-line arguments, deep links, Git, or
telemetry. It does not read the CC Switch database or export CC Switch configuration.
Automatic CC Switch provider creation is intentionally disabled because its public
deep-link/import contracts can expose a key outside the operating-system secret store.

The composer uses a verified cascade: recipient Agent, registered provider route, a model
available on that route, then a reasoning mode available for that exact model. Model
families and reasoning levels are separate fields; a model variant such as
`gpt-5.6-luna` must not be presented as a reasoning level. A routed message remains
`REQUESTED` until a recipient session whose observed runtime identity matches every
requested field acknowledges it.
A mismatched session cannot acknowledge the message. Only then does PeerBridge append
a SHA-bound `VERIFIED` route receipt.

Saved routes can be registered through MCP:

```json
{
  "route_id": "codex-luna-medium",
  "agent_id": "codex-main",
  "provider_id": "openai-official",
  "model_id": "gpt-5.6-luna",
  "reasoning_mode": "medium",
  "route_class": "official"
}
```

For a provider with a verified response alias, add the separate binding:

```json
{
  "route_id": "relay-grok-4.6",
  "agent_id": "grok-relay",
  "provider_id": "relay-grok-sui-xiang",
  "model_id": "grok-4.6",
  "response_model_id": "grok-4.6-build",
  "route_class": "relay"
}
```

`model_id` is the outbound request identity. `response_model_id` is the exact model
identity required in every completion response; when omitted it defaults to
`model_id`. Both are SHA-bound in the route profile and inference receipt.

Call `upsert_route_profile` with that payload, then select the profile in the monitor
or pass `route_profile_id` to `send_message`. Profiles and user-entered labels are
routing requests, not proof of upstream identity. Launch each MCP peer with its actual
observed labels, including `--reasoning-mode`, so the receipt gate can verify them.

## Recommended workflow

1. Each agent calls `bridge_status` and `workboard` before starting work.
2. The intended writer calls `claim_task` with precise read and write paths.
3. The writer calls `announce_work` while it works outside PeerBridge.
4. It records live hashes and test evidence with `record_proof`.
5. If the approval policy requires a peer, it calls `request_review`.
6. The peer reads the bound artifacts and calls `submit_review`.
7. The writer calls `complete_task`; PeerBridge rehashes the files and checks policy.
8. Anyone can call `verify_audit_chain` or run `peerbridge doctor`.

`request_review` is a manual governance queue. It never invokes a model and appears on
the Review page, not in room chat. To wake routed room Agents, post through
`post_room_message`: `once` sends one parallel round, while `discussion` runs bounded
parallel rounds. Replies never trigger another fan-out by themselves.

PeerBridge coordinates work. The coding clients still read, edit, and test files using
their normal tools.

## Approval modes

| Mode | Completion rule |
| --- | --- |
| `solo_allowed` | Live proof is sufficient. |
| `two_party_required` | An approved review from the configured peer is required. |
| `presence_aware` | Requires the peer while it is online; records a solo fallback when it is offline. |
| `quorum_required` | Requires `review_quorum` approvals from the configured `required_peers`. |

Presence-aware mode supports intermittent use. It avoids blocking all work when only one
paid agent is running while still requiring review when the named peer is actually live.
Quorum mode is intended for three or more independently connected agents. It does not
start, pay for, or authenticate those agents.

## Security boundaries

- `.peerbridge/` can contain conversation and task metadata. It is gitignored but not
  encrypted. Protect the project directory with operating-system permissions.
- Memory bodies are explicit coordination data, not an encrypted secret store. Never put
  credentials, hidden reasoning, or unrelated personal data in a memory record.
- Secret detection is a fail-closed best-effort filter, not a complete DLP system.
- The audit chain detects many mutations, but deletion of an unanchored tail cannot be
  proven from the database alone. Export or externally anchor important chain heads.
- A malicious local user with filesystem access is outside the current threat model.
- A local process can reach loopback and forge proxy headers; the operating-system
  account remains a trusted boundary for private mobile mode.
- PeerBridge never interprets a review as permission to run destructive shell commands.

Read the full [threat model](docs/threat-model.md) before using the bridge on sensitive
repositories.

## Non-goals

- Automatically waking or paying for another AI model.
- Replacing Git, code review, CI, or repository permissions.
- Applying generated patches.
- Hosting a remote multi-tenant MCP service.
- Encrypting the local SQLite database.
- Claiming that an AI review is equivalent to a human security review.

## Development

```powershell
python -m pytest
python -m compileall -q src
python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [architecture](docs/architecture.md), and the
[demo walkthrough](docs/demo.md). Future cloud and mobile work is explicitly separated in
the [roadmap](ROADMAP.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).

The PeerBridge name and logo are separate brand assets and are not licensed under
Apache-2.0. See [brand asset provenance](BRAND_ASSETS.md) and
[trademark guidelines](TRADEMARKS.md).
