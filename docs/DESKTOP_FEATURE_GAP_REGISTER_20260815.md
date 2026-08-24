# PeerBridge Desktop Feature Gap Register

Date: 2026-08-15 (Asia/Taipei)

This is the durable register for desktop, local collaboration, provider, mobile-control,
release, and commercial-preparation requirements discussed with the operator. It separates
implemented code from real acceptance evidence so that a context reset cannot turn an
unfinished feature into a completed claim.

## Status legend

| Status | Meaning |
| --- | --- |
| VERIFIED | Real evidence or an accepted focused gate exists. |
| IMPLEMENTED / REVERIFY | Code and focused tests exist, but current live UI/provider or full regression acceptance is incomplete. |
| PARTIAL | A lower-level contract or prototype exists; the requested end-user function is incomplete. |
| NOT STARTED | No complete implementation was found. |
| BLOCKED | A known failing gate prevents release acceptance. |

## Verified baseline

| ID | Capability | Live state and evidence |
| --- | --- | --- |
| B-01 | Local MCP coordination core | Dependency-free stdio MCP, shared SQLite state, leases, messages, reviews, proofs, and append-only audit chain are implemented and covered by tests. |
| B-02 | Multi-room Agent coordination | Persistent Agent identities, isolated rooms, room seats, room-scoped routing, bounded history, and human control-room primitives exist. |
| B-03 | Private phone transport | Tailscale HTTPS, loopback backend, identity allowlist, CSRF, scope isolation, rate limiting, and Funnel-off boundary exist. |
| B-04 | Real phone reconnect evidence | Evidence run `mobile-e2e-20260815-v1` is sealed: authenticated initial and reconnect sessions, the same device continuity hash, different browser-session nonce hashes, stable instance ID, and a 58-second disconnect gap. The formal release receipt is still pending (D-01). |
| B-05 | Credential boundary | Provider secret values are intended for OS credential storage; project DB/audit data stores identifiers and hashes rather than raw keys. Final release scanning remains pending (D-28). |
| B-06 | Local analytics primitive | Default-off, local-only aggregate analytics exists with DNT/opt-out handling and no network sender. Central usage counting is not implemented (D-25). |

## Desktop and local collaboration gaps

| ID | Operator requirement | Status | What exists now | Missing acceptance or work | Priority |
| --- | --- | --- | --- | --- | --- |
| D-01 | Finish real phone reconnect release evidence | PARTIAL | The evidence capture state and trace are sealed with a 58-second real reconnect. | Generate the create-only `remote-mobile-e2e-v2` receipt after source changes, then pass strict release verification. | P0 |
| D-02 | A usable, polished phone interface | NOT STARTED | A minimal engineering verification page can observe state and send a message. | Responsive room navigation, readable chat layout, Agent controls, touch ergonomics, accessibility, error states, and visual QA on real phones. | P0 |
| D-03 | Attach an image or file from the desktop composer | VERIFIED | The composer selects/clears files, validates supported image/text signatures, count and size limits, rejects credential-like text and symlinks, stages SHA-named copies under an ignored/protected local store, and binds them through direct, fanout, and first discussion messages. Focused tests and UI self-test pass. Physical Windows QA selected, staged, sent, and cleared a harmless text attachment in an automation-off disposable room with visible SHA acknowledgement and no external provider invocation. | Preview thumbnails and provider multimodal understanding are deliberately separate from this local Alpha. | P1 |
| D-04 | Let Agents actually receive and understand image attachments | IMPLEMENTED / PROVIDER-GATED | Codex receives native `localImage`; Claude receives native base64 image blocks; OpenAI-compatible routes receive image data URLs. Codex and Claude have a one-use hidden-token semantic challenge, while all routes keep transport evidence separate from proof of understanding. ACPX capability discovery is sanitized and persisted. Grok explicitly reports `image=false` and now fails before a turn; Kimi is installed but requires authentication before its capability can be read. Focused multimodal/runtime/workbench regression is 95/95 passing. | Obtain account-specific live semantic receipts for every image-capable route after authentication. Do not present Grok as image-capable unless its official runtime later advertises and passes that capability. | P1 |
| D-05 | Select and use Skills in a room or Agent seat | NOT STARTED | No complete Skill picker, binding, or provenance path was found. | Skill manifest selection, version/hash binding, permission preview, room/seat scope, invocation log, and portable context handoff. | P1 |
| D-06 | Traditional Chinese, Simplified Chinese, and English UI | VERIFIED | Persisted `zh-Hant`/`zh-Hans`/`en` catalogs, locale selector, fallback checks, translated chat/room/seat/attachment controls, focused tests, package UI self-test, and visible checks in all three locales pass. | Continue translating low-level engineering diagnostics after Alpha; they do not block normal desktop use. | P1 |
| D-07 | First-run tutorial and embedded help | IMPLEMENTED / REVERIFY | The current-panel help action opens a replayable localized guide covering all 12 panels. Each page uses a privacy-safe schematic with three numbered callouts and concrete steps; the provider guide explicitly distinguishes model routes from installed terminals. Focused tests and source UI self-tests pass at Windows 100%, 125%, and 150% scaling. | Recheck the guides in the new frozen QA executable, then record the approved 2-3 minute demo without adding a large video binary to Git history. | P1 |
| D-08 | Check for updates button | VERIFIED | An explicit read-only GitHub release checker displays published-release state, includes semantic Alpha tags even when published as normal GitHub Releases, ignores prereleases for a future Stable channel, and handles offline/error responses without installing code. The sidebar shows a path-free build digest, and an unpublished same-version QA build is never called the latest release. | Signed one-click update belongs to D-09 and is not claimed here. | P1 |
| D-09 | One-click signed update and rollback | NOT STARTED | No end-user updater exists. | Signature verification, staged download, process handoff, atomic install, rollback, audit receipt, and Windows packaging. | P1 |
| D-10 | Drag an Agent into a room and drag/remove it back out | VERIFIED | Drag handlers, ghost feedback, full-sidebar removal target, room-seat target, and history-preserving remove calls exist. Deterministic interaction tests prove leftward full-sidebar hit testing, drop-to-remove dispatch, and preselected-route drop-to-add dispatch. On 2026-08-15 the operator completed the physical add/remove workflow and explicitly accepted that it works. | Repeat only when changing drag geometry or supported Windows scale settings. | P0 |
| D-11 | Pick provider, model, and reasoning from each Agent card | VERIFIED | Per-seat route/model/reasoning controls and card menus exist. Physical Windows QA opened the live catalogs, committed `openai-official / gpt-5.6-luna / high`, then restored and committed `openai-official / gpt-5.3-codex-spark / high`; both states have bound membership SHAs. A dedicated regression proves the exact provider/model/reasoning binding survives Bridge restart and room switching. | Re-run only after changing seat-route UI or route persistence. | P0 |
| D-12 | Show the complete official Codex model catalog, including available Terra/Luna variants | VERIFIED | Live credential-free discovery and the physical card menu both displayed the seven official models: Sol, Terra, Luna, 5.5, 5.4, 5.4 Mini, and 5.3 Spark. The parsed catalog is SHA-bound in `LOCAL_ALPHA_CODEX_CATALOG_ACCEPTANCE_20260815.md`; parser/menu tests pass, and the physical Luna selection plus 5.3 Spark restore passed. | Re-run after official-client catalog schema changes. | P0 |
| D-13 | Add arbitrary relay/local providers using base URL, API key, and model | PARTIAL | OpenAI-compatible runner, route profiles, credential abstraction, and safe-connections UI foundations exist. | Complete guided onboarding, endpoint normalization, model discovery, health test, capability display, edit/delete/rotate flow, and live DeepSeek/Gemini-compatible validation. | P0 |
| D-14 | Reuse CC Switch providers beyond only Codex/Claude | PARTIAL | CC Switch adapter and model enumeration code exist without exporting raw credentials into project state. | Validate supported provider families, explicit import/refresh UX, collision handling, source identity labels, and live end-to-end receipts. | P1 |
| D-15 | Official Grok client/ACP path as a first-class Agent | VERIFIED TEXT/MCP; IMAGE UNSUPPORTED | Grok 1.0.1 initializes `grok-4.6`, performs real inference, invokes one PeerBridge MCP tool, and emits a sanitized provider-identity receipt. Its current ACP contract explicitly reports `image=false` and `audio=false`; PeerBridge rejects incompatible attachments before dispatch. | Preserve crash/recovery and identity receipts in the frozen build; re-open image support only after a future official runtime advertises and passes it. | P1 |
| D-16 | Real Claude, Grok, Kimi, and Codex collaboration | PARTIAL | Routing and provider-neutral collaboration contracts exist. A create-only bounded live room receipt now binds one real Grok 4.6 route and one real Kimi route, both replies, exact route receipts, a first-round consensus terminal state, and a zero-write re-verification. The control room now labels a bound route as `MCP NATIVE`, `MCP TOOL`, `INFERENCE`, or unverified instead of inferring capability from the Agent name; generic MCP-capable client and terminal registration is documented. | Add Claude and Codex to a later four-provider acceptance without weakening the honest distinction between MCP-native, bounded MCP tool-loop, and inference-only routes. | P0 |
| D-17 | Any authorized human or Agent room post wakes all routed room Agents | VERIFIED | Human-root receipt `room-wakeup-e2e-20260815-v1.json` records one terminal outcome per routed seat and no reply cascade. Agent-root receipt `agent-root-wakeup-e2e-20260815-v1.json` records a Grok-originated post, exactly one Claude reply and one Kimi reply, and zero reply dispatches. Both receipts re-verify with zero writes. | Preserve this invariant in regression and repeat only when fanout or dispatch semantics change. | P0 |
| D-18 | Agents discuss in parallel until consensus or a bounded stop | VERIFIED | Off/once/discussion modes, parallel rounds, consensus/blocker/stagnation/round/message limits, pause/resume/continue/stop, and no reply cascade are implemented in tests. Live task `mcp-capability-consensus-20260815123714` woke Grok 4.6 and Kimi in parallel; both completed exactly once and reached first-round consensus in about 35 seconds. Receipt `mcp-capability-discussion-e2e-20260815-v1.json` binds all four messages and exact route receipts and re-verifies with zero writes. Final physical QA applied bounded discussion, showed all termination controls, and restored automation off without dispatch. | Failure, crash-recovery, and no-hang behavior remain deterministic tests rather than a deliberately failed paid-provider run. | P0 |
| D-19 | Shared memory and context fusion across Agents | PARTIAL | Durable room/project/private memories and human-approved promotion exist. | Automatic bounded summarization/compaction, provenance-preserving retrieval, image/Skill-aware context, token budgeting, conflict handling, and cross-provider E2E. | P1 |
| D-20 | Keep desktop memory bounded during long multi-Agent work | VERIFIED | Bounded room history, supervisor parallelism, singleton locks, resource guard, operator-visible diagnostics, and a fresh SHA-bound 1,200-message soak passed with 409,600 bytes of private-memory plateau growth in its final eight samples, below the 24 MiB limit; verify-only performed zero writes. | Repeat a longer optional live-provider soak after public Alpha; the Codex desktop application's own memory remains outside PeerBridge control. | P0 |
| D-21 | Crash/recovery lifecycle gate | VERIFIED | Lifecycle, child-process terminal detection, crash/recovery, stale lease, singleton, duplicate-runtime rejection, and terminal dispatch reconciliation pass in the current full 907-test regression run (905 pass, 2 expected skips). An isolated copy of the authoritative schema-21 database also migrated to schema 22 and passed native `doctor` with all 37 pre-existing table payload hashes unchanged. | Preserve the gate in the final frozen-source run and every later release run. | P0 |
| D-22 | Publish-ready desktop visual polish | IMPLEMENTED / REVERIFY | Native WebView2 Modern Workbench is now the default Windows desktop and maps all twelve existing Control Room pages; Pixel remains an explicit `--legacy-pixel` compatibility surface. Source visual checks and the packaged headless WebView2/12-page self-test pass. | Repeat native visual checks at 100%, 125%, and 150% against the final frozen executable and bind the taskbar icon receipt. | P1 |
| D-23 | One-click private feedback that still works when provider/API-key onboarding is broken | VERIFIED FOR LOCAL ALPHA | A provider-independent Feedback page captures bounded diagnostics, supports explicit local encryption of a complete credential to the packaged release-bound public key, clears plaintext after use, and submits a sealed bundle through the maintainer-controlled HTTPS intake. A live synthetic case verified D1 metadata, private R2 storage, maintainer notification, exact download SHA, and local fallback behavior. | Preserve the 30-day retention job, administrative authentication, and live synthetic check before each release. | P0 |
| D-24 | Import selected local Agent conversation history as rooms | IMPLEMENTED / REVERIFY | Codex uses official app-server list/read; Grok uses `grok sessions list` plus its documented session record; Claude and Kimi use documented project session records. The UI lists bounded metadata first with zero default selections, supports up to 20 checked rows per operation, and also accepts explicit JSON/JSONL exports. Imports are create-only, source-SHA-bound, secret-redacted, paged, and read-only. Live metadata discovery returned Codex 50, Claude 11, Grok 50, Kimi 0 on the current workstation. | Re-run the four-provider selection UI against the final frozen executable; Kimi remains an honest empty state until a local Kimi session exists. | P0 |

## Remote, release, and business-preparation gaps

| ID | Operator requirement | Status | What exists now | Missing acceptance or work | Priority |
| --- | --- | --- | --- | --- | --- |
| R-01 | Full mobile/PWA control room | PARTIAL | Tailnet-only human observation/message page works. | Room/Agent management, approvals, reconnect UX, installable PWA, offline state, and mobile notifications. | P1 |
| R-02 | Authenticated remote MCP for Agents | NOT STARTED | Current remote page is a narrow human control plane, not general remote MCP. | Streamable HTTP MCP, per-Agent identities/scoped tokens, replay protection, tenant isolation, and threat-model review. | P2 |
| R-03 | Storage abstraction and PostgreSQL option | NOT STARTED | Local SQLite is authoritative. | Storage interface, PostgreSQL implementation, migrations, concurrency tests, and identical audit semantics. | P2 |
| R-04 | Transparent central opt-in usage analytics | NOT STARTED | Local daily aggregate analytics is default-off and has no sender. | Separate consent, self-hostable HTTPS collector, coarse allowlisted events, retention/deletion controls, public schema, and admin aggregate view. No prompts/files/keys may be collected. | P2 |
| R-05 | GitHub download and usage dashboard | NOT STARTED | Documentation correctly states Release API `download_count` is downloads, not users. | Fetch/archive release asset counts, show trends, keep them separate from opt-in installations and authenticated accounts, and document metric limitations. | P2 |
| R-06 | Windows installer and signed release pipeline | PARTIAL | Strict wheel/sdist validation passes. A create-only Windows x64 portable ZIP passes fresh extraction, packaged UI self-test, frozen MCP send, initialization, and `doctor`; exact final hashes are generated after source freeze. | Native signed installer, code signing, SBOM, install/uninstall, upgrade/rollback, and signed-update provenance remain post-Alpha work. | P1 |
| R-07 | Final security/privacy and secret-leak audit | VERIFIED | The current source snapshot, one-commit Git history, wheel, and sdist were scanned for credential shapes, private keys, personal absolute paths, unsafe package members, and sensitive tracked filenames. No credential, private-key, or personal-path finding remains; the only token-shape match is a benign long Python test identifier. The dependency-free core and Apache-2.0 package metadata are intact. | Re-run the same strict gate after any source change and before operator-approved publication. | P0 |
| R-08 | Complete documentation and public launch guide | VERIFIED FOR ALPHA | README, branding/trademark notice, contributor/security material, architecture, threat model, remote, telemetry, provider-routing, open-core docs, an operator-ready GitHub Alpha release draft, troubleshooting, support matrix, English instructions, and public Traditional/Simplified Chinese quick starts exist. | A polished demo video is deferred and is not required to install or audit this Alpha. Recheck every release link and final artifact hash before publication. | P1 |
| R-09 | Commercial boundary without crippling the open core | PARTIAL | Open-core boundary and dormant capability manifest exist; no commercial service is active. | User validation, managed-service design, entitlement/billing outside the core, privacy/legal review, and explicit separation of experimental self-hosted remote from future paid hosting. | P2 |
| R-10 | Cloud sync, managed remote, and mobile push | NOT STARTED | Capability IDs are declared as unavailable future services. | No backend, account system, billing, managed sync, or push service has been implemented. | P2 |

## Alpha 5.2 approved requirement delta

Date added: 2026-08-19 (Asia/Taipei)

These rows extend the historical register. Their candidate implementation states remain
subject to the final tagged-source release gates. Detailed normative requirements are in
[`ALPHA_5_2_REQUIREMENTS.md`](ALPHA_5_2_REQUIREMENTS.md).

| ID | Operator requirement | Status | Existing foundation | Required Alpha 5.2 outcome | Priority |
| --- | --- | --- | --- | --- | --- |
| A52-01 | View several independent backend AI CLI sessions on one PeerBridge page | IMPLEMENTED / RELEASE GATE | Control Room, Agent Library, room Seats, routes, and presence exist. | Stable Agent Cockpit panels with Terminal, Activity, Answer, Evidence, Grid, Focus, and Timeline views. | P0 |
| A52-02 | Launch and control managed CLI sessions without claiming access to hidden state | REVIEWED PROFILES | CLI discovery, provider runners, process-tree control, and supervisor lifecycle primitives exist. | Codex read-only and Claude Code plan-mode profiles plus deny-all ACPX profiles for installed Kimi Code and Grok CLIs provide bounded capture, shared redaction, stdin-only input, start/send/interrupt/stop, and honest observable-output limits. | P0 |
| A52-03 | Match the useful local collaboration outcomes of competing rooms and command centers | IMPLEMENTED / RELEASE GATE | Rooms, presence, threads, attachments, tasks, one-round fanout, bounded discussion, and tutorial exist. | Four workflow templates, persistent bounded room policies, a durable operation queue, crash reconciliation, and opt-in scheduling share one local UI. | P0 |
| A52-04 | Isolate writers and govern Skills/MCP tools | IMPLEMENTED / RELEASE GATE | Path leases, resource slots, allowlisted runner tools, and permission receipts exist. | Exact Git worktrees, same-source review bindings, a versioned Skill/MCP registry, scoped grants, and sensitive-action approval remain human-governed. | P0 |
| A52-05 | Make approved decisions binding, typed, and traceable | IMPLEMENTED / RELEASE GATE | Source-bound Private/Room/Project memory and human-only Project promotion exist. | Typed decisions, supersession/revocation, applicable task briefing, briefing hashes, and decision-conflict findings are append-only. | P1 |
| A52-06 | Surface the PeerBridge trust model as an end-user workflow | IMPLEMENTED / RELEASE GATE | Task leases, proof, source-bound reviews, live rehash, route receipts, and audit chain exist. | Trust Timeline, immediate stale evidence, bounded disagreement recheck, sanitized Proof Bundle, and independent verification are exposed in Control Room. | P0 |
| A52-07 | Prepare for cloud continuity without silently enabling cloud collaboration | IMPLEMENTED / DISABLED | Separate Cloudflare announcement and encrypted-feedback services exist; Tailscale provides narrow private mobile control. | A versioned metadata-only local event envelope exists; local SQLite remains authoritative and cloud collaboration has no transport in 5.2. | P1 |

## Current release blockers and explicit Alpha limitations

1. R-07: the Alpha 5.2 working tree passes the 907-test Python regression (905 passed, two
   expected skips), compileall, development/build checks, the three-session identity check,
   Node 22.22.0 Edge (43/43) and receiver (15/15) tests, six source UI combinations, six
   packaged UI combinations, independent portable verification, and isolated schema-21 to
   schema-22 native migration. Independent review, the final frozen-source regression,
   continuity refresh, clean annotated-tag strict check, final portable provenance, and CI
   remain release gates.
2. D-16: the real Grok/Kimi discussion receipt is sealed. A later four-provider acceptance
   remains an explicit Alpha limitation rather than a fabricated pass.
3. R-06: wheel/sdist and a Windows x64 portable candidate are validated; a signed native
   Windows installer remains an explicit post-Alpha limitation rather than a fabricated gate.
4. D-23: local public-key encryption, the HTTPS/D1/private-R2 intake, maintainer notification,
   exact bundle re-download verification, and encrypted local fallback are all verified for
   this Alpha.

Remote/mobile D-01 and R-01 are deliberately outside the **local Alpha** release profile.
Their existing evidence is preserved and is not deleted or presented as local Alpha evidence.

## Acceptance order

1. Freeze the local Alpha source after the now-passing D-03/D-10/D-11/D-12/D-18/D-23 local
   acceptance and independent code review.
2. Preserve the passing full regression, six-combination source/package UI self-tests,
   schema migration, memory soak, and
   secret/path/privacy/license scan evidence.
3. Preserve the strict wheel/sdist build and extracted-artifact CLI/UI smoke evidence.
4. Update the continuity manifest and honest Alpha limitations.
5. Publish only after every frozen-source gate passes and the operator has provided a current
   authorization for this Alpha.
6. Resume phone UI, cloud sync, paid remote service, and trading work in their preserved,
   separate scopes after the local Alpha deadline.

This register is append-oriented project history. When a row changes state, record the date,
evidence path, and acceptance result rather than silently deleting the original requirement.
