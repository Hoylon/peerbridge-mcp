# PeerBridge Alpha 5.2 Requirements

Target tag: `v0.1.0-alpha.5.2`

Target package version: `0.1.0a5.post2`

Status: approved target requirements. Nothing in this document is an implementation or
release claim until the corresponding acceptance evidence exists.

Current acceptance status: the approved feature scope is implemented in the live working
tree; its focused and full Python gates, Node 22.22.0 Edge/receiver contracts, and development
and build checks pass. Final frozen native UI acceptance, continuity refresh, Windows
portable verification from final source, tagged-source, CI, and publication gates remain
required. This is not a release claim.

## Product objective

PeerBridge Alpha 5.2 must make heterogeneous AI coding Agents understandable and operable
from one local control room while preserving the evidence, identity, and human-governance
rules that distinguish PeerBridge from a terminal multiplexer or chat room.

The primary experience is:

> Launch or connect several supported AI CLIs, see their independent live work on one
> PeerBridge page, coordinate a task, and verify exactly what state each answer reviewed.

Alpha 5.2 follows two requirements at the same time:

1. Reach parity with the useful local outcomes offered by multi-Agent rooms, command
   centers, dual-Agent workflows, decision ledgers, and durable control planes.
2. Exceed those outcomes through source-bound evidence, live-state verification, explicit
   route identity, scoped authority, and auditable human decisions.

## Existing foundation

Alpha 5.2 must reuse rather than replace the verified local core:

- global Agent identities, room-scoped Seats, presence, threads, attachments, and rooms;
- one-round fanout and bounded discussion without recursive reply cascades;
- task leases, path conflict checks, proof, source-bound review, and live rehash;
- provider-neutral Private, Room, and human-approved Project memory;
- route and inference receipts that separate requested identity from observed identity;
- a local SQLite authority, bounded resource controls, and a SHA-linked audit chain;
- the existing Cloudflare announcement and private-feedback services, which remain
  isolated from future collaboration sync.

## Release requirements

### A52-01: Unified Agent Cockpit

- Add an `Agent Cockpit` page inside the PeerBridge Control Room.
- Aggregate three truthful conversation sources without flattening their different
  capabilities: PeerBridge native rooms, authorized desktop application conversations,
  and terminal/CLI conversations.
- Let the operator list bounded conversation metadata from supported local Agents and
  explicitly check which conversations to import. Listing must select nothing by default;
  unselected conversations must not have their full content read or persisted. Imported
  conversations are create-only, source-SHA-bound, secret-redacted, and read-only.
- Let a user choose an Agent on the Conversation page and open that exact Agent in the
  Cockpit. The link must preserve the source conversation/session identifier rather than
  copying or reconstructing its context.
- Show several independent Agent sessions at once with stable panel identity. Completion
  order must never move one Agent's output into another Agent's panel.
- Support Grid, Focus, and Timeline views with responsive tabs when the window is too narrow
  for readable columns.
- Each panel exposes separate Terminal, Activity, Answer, and Evidence views.
- Display truthful session state, client, requested route, observed route, model, role,
  elapsed time, bounded usage data, and terminal outcome when available.
- Display a provider-neutral capability contract for every source: detectable, mirrorable,
  input-capable, context-resumable, terminal-controllable, and model-route-only. An
  unavailable capability must remain visibly unavailable rather than being inferred from
  an installed process or model route.
- Keep an externally opened desktop or terminal window open when it is mirrored. Mirroring
  must not imply transfer of input authority. At most one surface may hold write/input
  control for the same session at one time, with an explicit operator handoff.
- Never label hidden chain-of-thought as visible. Only captured terminal output, tool
  events, explicit plans, progress, reasoning summaries, and final answers may be shown.

### A52-02: Conversation participants and roles

- Keep Agent participation and role assignment on the Conversation page. Do not create a
  second competing participant/role editor in the Cockpit.
- Default every Agent to `Equal participant`. Offer Researcher, Implementer, Reviewer, and
  an explicit custom role. A role describes division of work only and never grants extra
  permission, authority, tool access, or voting weight.
- Persist the role on the exact room membership and include it in room membership receipts
  and audit events. A role change must not replace the Agent identity or room session.
- Provide `View live work` from the selected room participant. If the source cannot be
  mirrored, the Cockpit must still focus its truthful detected/unsupported state and explain
  the missing capability without inventing output.

### A52-03: Managed local Agent sessions

- Discover supported installed CLIs without reading their credentials or private history.
- Launch supported CLIs as PeerBridge-owned sessions with explicit project, Agent, role,
  route, and working-directory bindings.
- Keep reviewed profiles provider-specific. Codex app-server, Claude Code stream-json, and
  installed Kimi Code or Grok ACPX sessions use persistent official protocols. Observe and
  Review remain read-only. Edit and Full development require an active human-approved
  governed Git worktree and bind the exact governance record. Edit keeps normal Agent
  networking and maps to the official client's standard permission contract; unsupported
  escalation fails closed rather than silently switching modes. Full development requires
  one explicit session-start decision, records that bounded authorization, and enables the
  complete provider tool set only until the session ends. The UI must distinguish a governed
  worktree from an OS-level sandbox and warn when Full access uses the local account boundary.
- Capture bounded stdout, stderr, structured events, and lifecycle state with shared secret
  redaction before display or persistence.
- Bound the total retained authorized-session records per local scope. New records may
  prune only terminal or adapter-stale sessions; if all retained records are live, the
  adapter must fail closed rather than evicting an active session.
- Expose per-session sequence cursors so the Cockpit and authorized adapters read only
  new events; when a cursor falls behind the retained window, return the bounded retained
  window and mark the observable history as truncated.
- Cap each observable-session list at 512 events and cap the complete serialized MCP/JSON-RPC
  response, including session contracts and wrappers, at 8 MiB of UTF-8. Truncation must
  preserve per-session event order and sequence-cursor continuation.
- Provide start, send, interrupt, stop, and crash-recovery controls with exact process-tree
  ownership. PeerBridge must not terminate an unrelated process.
- An externally opened terminal may be shown as `Detected` or connected through an explicit
  adapter, but PeerBridge must not claim it can recover output that it never captured.
- Viewing an existing session must not require selecting a working directory. A working
  directory is required only when PeerBridge is explicitly asked to launch a new managed
  process, and should normally be inherited from the originating conversation/workspace.
- Preserve a dependency-free coordination core. Platform terminal support belongs to the
  desktop/runtime boundary and must fail closed when unavailable.

### A52-04: Local collaboration parity

- Provide a five-minute path from first launch to one completed multi-Agent workflow.
- Reuse rooms, presence, mentions, threads, attachments, task cards, and bounded discussion
  in the Cockpit instead of creating a second incompatible collaboration model.
- Provide Implement + Review, Investigate + Debate, Read-only Audit, and Release Gate
  workflow templates.
- Bind the first guided workflow operation to the exact room discussion, route, prompt, and
  source state. A successful launch is not completion: only a `completed` discussion may
  succeed, while cancellation, timeout, worker loss, restart, retry, or source drift must
  reconcile fail-closed without opening a duplicate managed CLI workflow.
- Support persistent listening/wake-up only through bounded, operator-visible policies.
- Add a durable local operation queue with cancellation, timeout, retry classification,
  resource keys, and crash reconciliation.
- Add opt-in scheduling for saved workflows. Scheduled work must use the same permissions,
  leases, evidence, and stop controls as interactive work.

### A52-05: Isolated and governed execution

- Give each writer an isolated Git worktree by default when Git is available.
- Bind implementer output and reviewer input to the same commit or diff hash.
- Keep patch application and merge as explicit human-controlled actions.
- Add a versioned Skills and MCP Tool Registry with per-Agent and per-Room allowlists.
- Record permission decisions and require human approval for configured sensitive actions.
- Compose with existing tools such as GitHub MCP instead of reimplementing entire external
  service APIs inside the trust kernel.

### A52-06: Decision and memory governance

- Extend explicit memory records with FACT, DECISION, CONSTRAINT, PREFERENCE, and DEPRECATED
  types while preserving Private, Room, and Project visibility.
- Bind every promoted Project decision to an explicit source and human authority.
- Support supersession and revocation without silently rewriting history.
- Brief a task with only applicable approved records and record the briefing hash.
- Detect conflicts with binding decisions as review findings, not as automatic claims that
  a patch is unsafe.

### A52-07: Visible trust and evidence

- Add a Trust Timeline from task claim through execution, tests, proof, review, human
  decision, and completion.
- Mark evidence stale immediately when its bound live state changes.
- Show substantive Agent disagreements with their exact evidence rather than reducing them
  to majority voting.
- Allow a bounded recheck of the disputed test or artifact against the exact source state.
- Export a sanitized, portable Markdown and JSON Proof Bundle that requires a separately
  installed trusted verifier and never executes code carried inside the bundle.
- The bundle must bind identities, source hashes, tests, reviews, decisions, and audit-chain
  position without containing credentials or unrestricted terminal history.
- Start the local verification trigger engine with the Control Room, not when the Trust page
  is opened. Due schedules, stale source bindings, failed checks, permission-sensitive work,
  and explicit release requests must create or update visible verification work in the
  background.
- A release requested through PeerBridge must first materialize a source-bound `Release
  Gate` operation. PeerBridge must block its own release action until that exact gate is
  fresh and successful and the required human decision exists. Opening the Trust page is
  only for inspection, intervention, and approval; it is not the trigger.
- Automatic triggering must be idempotent and bounded. It must not duplicate work on every
  refresh, silently approve a result, or repeatedly spend provider quota after a terminal
  failure. External release commands that bypass PeerBridge cannot be intercepted locally,
  so repository CI remains an independent enforcement boundary.

### A52-08: Cloud-ready boundary

- Define a versioned, local event envelope suitable for later encrypted synchronization.
- Keep local SQLite authoritative for Alpha 5.2 and keep cloud collaboration disabled.
- Do not reuse feedback or announcement routes, D1 tables, R2 objects, retention rules, or
  credentials for collaboration data.
- Do not implement public remote shell control, hosted model execution, accounts, billing,
  or multi-tenant collaboration as part of Alpha 5.2.

### A52-09: Understandable local operation

- Keep the complete twelve-panel navigation discoverable at the minimum supported window
  size with a permanently visible scrollbar, direction controls, and a localized prompt
  that more panels are available below.
- Keep managed-CLI launch controls in responsive rows at the 980 px minimum window across
  the supported 100%, 125%, and 150% Windows display scales.
- Use action labels that describe the actual result rather than generic words such as
  `Run`, `Open`, or `Info` where the target would otherwise be ambiguous.
- Provide a replayable, localized illustrated guide for every panel. Each guide must open
  on the current panel, use privacy-safe schematics rather than real user data, and link
  its numbered callouts to concrete operating steps.
- Distinguish an available model route from a locally installed Agent terminal everywhere
  the two can be confused. Talking to Grok or Kimi through a route must never be presented
  as proof that its CLI was downloaded.
- Never install an Agent terminal merely because a route exists or was used. Kimi Code
  installation requires an explicit button press and confirmation; Grok remains a verified
  official-guide path until a publisher-verifiable Windows installer is available.
- Let the local usage dashboard switch between Today, Last 7 days, Last 30 days, and All
  time. Use hourly, daily, daily, and monthly buckets respectively, and apply the selected
  period consistently to API usage summaries, provider/model breakdowns, charts, and tables.
- Use the native WebView2 Modern Workbench as the default Windows desktop and map all twelve
  existing Control Room pages into it. Keep Pixel Control Room as an explicit
  `--legacy-pixel` compatibility surface. Neither surface may download executable theme code
  or copy third-party product branding.

## Acceptance gates

Alpha 5.2 is not release-ready until all of these are true:

1. At least three supported managed sessions can run concurrently without output crossing,
   unbounded memory growth, duplicate dispatch, or orphaned process trees.
2. A fresh Windows installation completes the first verified workflow within five minutes
   without requiring the operator to understand route, Seat, lease, or receipt internals.
3. Review, proof, and completion bind the same exact source state; any later change makes
   the old evidence visibly stale.
4. Crash, cancellation, timeout, restart, and retry behavior pass deterministic lifecycle
   tests and preserve exactly-once terminal outcomes.
5. Credential-shaped data is redacted before any Cockpit persistence or Proof Bundle export;
   release scans find no real secret or private absolute path.
6. Existing room isolation, memory visibility, route verification, feedback privacy,
   Cloudflare Edge, full Python, Windows portable, and strict release gates remain green.
7. Physical UI checks pass at supported Windows scaling levels with no overlapping text,
   unusable terminal panes, or inaccessible controls.
8. Documentation distinguishes captured work from hidden reasoning and distinguishes a
   managed session from a merely detected external process.
9. A new user can discover every panel, open its illustrated guide, distinguish model
   routes from local terminals, and change the usage period without relying on external
   documentation.
10. Room roles persist without changing Agent identity or authority, and `View live work`
    focuses the exact source-bound Agent session from the Conversation page.
11. Native-room and managed-CLI sources render through the same provider-neutral session
    contract; desktop sources truthfully expose only capabilities supported by their
    authorized adapter.
12. An explicit PeerBridge release request creates exactly one matching Release Gate before
    any PeerBridge release action can proceed, even if the Trust page has never been opened.
13. Codex, Claude, Grok, and Kimi history discovery selects zero conversations by default;
    importing one or more checked conversations preserves source identity and SHA, redacts
    secret-shaped text, and keeps the resulting history room read-only.
14. A packaged Windows executable passes the headless native-workbench test for WebView2,
    loopback binding, all twelve navigation pages, and the exact frozen runtime SHA.

## Later public releases

- Alpha 6 hardens the execution platform and closes any 5.2 compatibility debt without
  weakening the local trust kernel.
- Beta 1 introduces the opt-in cloud continuity service: encrypted multi-machine rooms,
  remote Cockpit observation and approvals, device pairing, notifications, and external
  audit anchors. Actual Agent execution remains on an authenticated desktop or runner.
- 1.0 adds stable APIs and migrations, evidence-derived capability routing, calibrated
  Agent selection, collaboration replay, failure attribution, and an open evidence receipt
  protocol after sufficient real-world evidence exists.

## Permanent boundaries

- No extraction or fabrication of hidden chain-of-thought.
- No API key, token, cookie, or raw credential in messages, memory, SQLite, terminal
  archives, telemetry, cloud sync, or proof exports.
- No automatic destructive command, patch application, merge, or remote shell authority.
- No reputation or capability score presented as reliable before there is sufficient,
  task-class-specific evidence and an explicit uncertainty model.
- No claim that an AI review is equivalent to a human security review.
