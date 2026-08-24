# PeerBridge Local Alpha Support Matrix

This matrix describes the `0.1.0-alpha.5.2` local Alpha. A test or implementation entry
does not upgrade a provider route to verified official identity.

## Platforms and distribution

| Area | Alpha state | Notes |
| --- | --- | --- |
| Windows 10/11 | Supported candidate | Primary development and physical QA platform. |
| Windows x64 portable ZIP | Release-gated | The published asset is accepted only after tagged-source build, fresh create-only extraction, UI self-test, frozen MCP send, initialization, and `doctor`; the executable is not code-signed. |
| Python | 3.11+ | Dependency-free coordination core; encrypted feedback uses the optional `feedback` extra. |
| Linux/macOS | Core portable; macOS CI-gated | The stdio/SQLite core is portable. The `macos-14` matrix runs the full core and a live Seatbelt write-denial contract; a signed/notarized macOS desktop artifact is not included. |
| Wheel and source distribution | Release-gated | Publication requires the annotated tag, a clean source tree, strict package inspection, and reproducible wheel/sdist builds. |
| Native Windows installer | Not included | The portable ZIP is not an installer. No signed installer, automatic updater, or rollback yet. |
| Remote/mobile | Experimental, excluded | Existing tailnet evidence is preserved but is outside the local Alpha profile. |

## Clients, providers, and models

| Integration | State | Evidence boundary |
| --- | --- | --- |
| Codex MCP client | Verified local Alpha | Official ACP client `@agentclientprotocol/codex-acp` 1.6.2 initialized a real `gpt-5.6-sol` / high session and invoked exactly one PeerBridge `bridge_status` tool with a sanitized provider-identity receipt. |
| Official Codex models | Implemented / account-dependent | Current discovery returned Sol, Terra, Luna, 5.5, 5.4, 5.4 Mini, and 5.3 Spark variants with model-specific reasoning choices. Availability remains client/account dependent. |
| Claude Code MCP client | Verified local Alpha | Native Claude Code 2.1.201 initialized a real `claude-sonnet-5` session, invoked exactly one PeerBridge `bridge_status` tool, and passed the independent zero-write receipt check. |
| Kimi Code CLI | Installed; authentication required | Kimi Code 0.38.0 is installed through the pinned, hash-verified flow and has a first-class ACPX profile with scoped Edit and separately confirmed Full access. A live startup reaches the official CLI but stops before session creation with the sanitized terminal state `Authentication required`; capability discovery and inference remain blocked until the operator signs in. |
| OpenAI-compatible relay/local endpoint | Implemented / guided onboarding | Direct routes do not require CC Switch. The Connect page saves or rotates a credential in Windows Credential Manager, discovers advertised model IDs, and creates explicit Agent routes without copying secrets into SQLite. |
| CC Switch import | Implemented / profile-dependent | The Connect page uses the public CLI to list all six supported CC Switch application families, fetch model IDs, create relay-class routes, and perform an explicitly confirmed provider switch. Missing CC Switch endpoint, credential, or quota is reported without silently falling back. |
| Official Grok ACP/client | Installed and capability-verified; current quota-limited | Grok initializes the official `grok-4.6` ACP contract and advertises `promptCapabilities.image=false` and `audio=false`, so unsupported media fails before a model turn. A prior tool receipt remains valid, while the current account's fresh inference is visibly blocked by its rolling free-usage limit. PeerBridge does not reinterpret that provider quota as a local failure or silently fall back to a relay identity. |
| DeepSeek/Gemini-compatible routes | Contract only until configured | Register as explicit relay/local routes; no provider identity is inferred from a model string or API shape. |

`official`, `relay`, and `local` are evidence classes. Relay names and model response strings
do not prove the upstream operator. PeerBridge records requested, expected-response, and
observed model identities separately and fails closed on an unconfigured mismatch.

## Desktop capabilities

| Capability | Alpha state | Limitation |
| --- | --- | --- |
| Agent Cockpit | Implemented / Windows QA | The terminal-first Grid shows Codex, Claude Code, Grok, and Kimi side by side with sanitized terminal output, follow-up input, attachments, and lifecycle controls. The primary runtime strip exposes each Agent's actual model/route and governed permission tier, with red offline, green online, amber working, and blue waiting states derived from local presence, sessions, dispatches, and observable events. Focus retains one session's Terminal, Activity, Answer, and Evidence identity; Timeline orders events across all sessions. Only captured observable actions are shown; private reasoning content is never inferred or exposed. |
| Read-only code diff | Implemented / bounded | The Changes page reads the current Git worktree without acquiring Git locks, displays per-file additions and deletions plus a colored unified diff, excludes private runtime and credential-file pathspecs, redacts secrets and machine-private absolute paths, and enforces response-size and file-count limits. It never stages, commits, or edits files. |
| Managed official sessions | Four governed profiles | Codex app-server, Claude Code stream-json, Grok ACPX, and Kimi ACPX expose persistent start/send/interrupt/stop/resume contracts. Observe/Review are read-only. Edit is network-capable and maps to provider-native standard permissions. Full access records one bounded session authorization and enables the complete provider tool set only for that session. Both write tiers require an active human-approved governed worktree. |
| Agent conversation history import | Implemented, explicit opt-in | Codex lists/reads via official app-server, Grok lists via `grok sessions list`, and Claude/Kimi use their documented project session records. Only bounded metadata is read during listing, no row is selected by default, and full content is read only for checked conversations. Imports are create-only, source-SHA-bound, secret-redacted, and read-only. Continue from history creates a separate writable room with a bounded source ID/SHA context message; it never mutates the imported source room. JSON/JSONL file import is also available. |
| Concurrent managed sessions | Verified local contract | Three sessions with different completion order retain isolated output; runtime capture, event count, line size, retained sessions, and process ownership are bounded. Codex/Claude raw provider output is capped at 8 MiB per persistent session and ACPX stdout/stderr at 4 MiB per turn. |
| Workflow operations | Implemented | Four templates use a durable local queue with cancellation, timeout, retry classification, crash reconciliation, and opt-in interval scheduling. Scheduling does not bypass permissions. |
| Isolated execution and permissions | Provider-specific and explicit | Human-approved Git worktrees use exact repository, Git common-directory, commit, and diff bindings. Codex uses native workspace-write with on-request escalation in Edit. Claude Edit uses accept-edits with shell escalation not pre-authorized. Grok/Kimi Edit permits read/search/edit/fetch through the ACP host and rejects execute/delete/move escalation. Full access is a separately confirmed trusted-session mode. Optional WSL2 and macOS sandboxes remain defense in depth. |
| Windows Agent executable trust | Mixed, reported explicitly | Codex and Claude launch only after resolving to valid OpenAI/Anthropic Authenticode-signed binaries. For npm-delivered Codex, Kimi, and ACPX packages, one-click install stages the exact pinned version, verifies the downloaded tarball bytes against the reviewed SHA-512, revalidates file identity, and installs that local archive; lifecycle scripts are disabled except for Kimi's reviewed required `postinstall`. Kimi and ACPX shims still lack a Windows publisher signature, so PeerBridge does not label them as signature-verified. The trusted Windows user-account boundary remains required. |
| Typed decisions and task briefing | Implemented | FACT, DECISION, CONSTRAINT, PREFERENCE, and DEPRECATED records support applicability, supersession, revocation, briefing hashes, and conflict findings. |
| Trust Timeline and Proof Bundle | Implemented | Completion requires fresh test, proof, review, and human-decision evidence. Bundles are create-only, sanitized, and source-bound. Verification must use a separately installed trusted PeerBridge copy; an unsigned bundle proves structural consistency, not sender identity. |
| Skill/MCP capability governance | Implemented registry | Versioned capabilities and per-Agent/per-Room grants are auditable; external service APIs are composed rather than copied into the trust kernel. |
| Multiple rooms and reusable Agent seats | Implemented | Context does not silently cross rooms. |
| Root-post room wakeup | Verified local Alpha | Human and Agent root-post receipts prove routed fanout; offline seats terminate rather than hang. |
| Bounded parallel discussion | Verified local Alpha | A live Grok/Kimi round reached consensus; consensus, blocker, stagnation, round, message, and timeout limits prevent loops. |
| Per-seat provider/model/reasoning | Verified local Alpha | Physical apply/restore and persistence checks pass; choices remain provider- and model-specific. |
| Drag add/remove | Verified local Alpha | Physical add/remove and deterministic drag tests pass; removal preserves history and requires confirmation. |
| Traditional Chinese / Simplified Chinese / English | Supported | Low-level engineering diagnostics may remain English in Alpha. |
| Native Modern Workbench / Pixel compatibility | Supported | A fresh Windows workspace opens a local first-run chooser with built-in Pixel Control Room and Modern Workbench previews. The selection is saved locally and can be changed later from Modern Appearance or the Pixel toolbar; restart applies the other desktop surface. `--legacy-pixel` remains an explicit compatibility override. Neither surface downloads theme code or changes coordination authority. Pixel combobox fields and popdowns use the same dark palette so readonly values remain legible. |
| First-run tutorial | Supported | Guides the reviewed Cockpit path and keeps provider setup separate. The tutorial is centered over the active Control Room, including on negative-coordinate displays, and announcements wait until it closes. Screenshots/video are not bundled yet. |
| Read-only update check | Supported | Alpha checks include published GitHub releases; no automatic installation. |
| Image/text attachment transport | Implemented with capability-gated semantics | Chat, initial Agent instructions, persistent session turns, and Feedback accept selected files or pasted clipboard images under the same 5-file/16 MiB limits. Codex app-server receives native `localImage`; Claude stream-json and Claude-compatible routes receive native base64 image blocks plus bounded verified text; both have a one-use semantic image challenge that records success only when the model returns the hidden image token. OpenAI-compatible routes receive native image data URLs and produce transport receipts, but transport alone is not treated as semantic proof. Grok ACP currently advertises `image=false` and fails before the turn; Kimi is installed but authentication blocks capability discovery. Unsupported and unauthenticated routes terminate visibly instead of spinning. |
| Private feedback | Verified local Alpha | The packaged HTTPS intake validates a sealed bundle, stores it temporarily in private R2 with bounded D1 metadata, and sends a maintainer notification. Normal diagnostics redact secrets; complete-key escalation is explicit and locally encrypted before upload. Local retry fallback is preserved. |
| Usage analytics | Off by default, local only | No central sender and no claim of unique-user counts. The Workbench can switch Today/7-day/30-day/All-time views; totals, horizontal bars, four-line trends, provider and model breakdowns use the same selected period. |
| Cloud collaboration | Disabled in Alpha 5.2 | A metadata-only local event envelope is defined for future encrypted continuity, but there is no collaboration transport, account, billing, tenant, or remote-shell service. Feedback and announcements remain separate data planes. |

The Windows acceptance matrix covers 100%, 125%, and 150% display scaling. Passing source
layout checks does not replace the final native first-run and taskbar-icon review.

## Resource and data boundaries

- SQLite is authoritative for local coordination; PostgreSQL and cloud sync are not included.
- One singleton supervisor coordinates provider runners. Duplicate writers are rejected.
- Room history, parallelism, discussion budgets, and provider subprocess resources are
  bounded. The passing local soak does not cover the desktop client's own memory.
- `.peerbridge/` and `.peerbridge-artifacts/` are private runtime data and must not be
  published or attached to public reports.
- Provider credentials and complete private endpoints do not enter SQLite, MCP messages,
  normal feedback, analytics, receipts, or Git history.

## Release interpretation

This is an honest Alpha support matrix, not a Stable compatibility promise. Features marked
`physical reverify`, `live-provider reverify`, `partial`, or `contract only` remain visible
limitations until a current receipt closes the stated boundary.
