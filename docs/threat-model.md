# Threat Model

## Assets

- Project files referenced by messages, tasks, reviews, and proofs.
- Coordination metadata in `.peerbridge/peerbridge.sqlite3` and its WAL files.
- Task lease capability tokens returned to clients.
- Audit-chain integrity and externally recorded chain heads.
- Human decisions and peer review findings.
- Provider API keys and private endpoints held by an operating-system or CC Switch vault.

## Trust assumptions

- The operating system account and filesystem are trusted.
- MCP clients are authorized to see the project they are connected to.
- A local administrator can bypass PeerBridge and is outside the security boundary.
- The Python interpreter and installed package are trusted.
- SQLite correctly enforces local transactions and locking.

## Defended cases

| Risk | Mitigation |
| --- | --- |
| Two agents edit overlapping paths | Transactional read/write path conflict checks. |
| Dead agent keeps a task forever | Time-bounded lease and deterministic expiry recovery. |
| One consumer hides a message from another | Per-consumer receipts and cursors. |
| Completion uses stale files | Live SHA-256 rehash at proof recording and completion. |
| Review references different files | Project-relative artifact bindings with live hashes. |
| Path traversal | Normalization and project-root containment checks. |
| Obvious credential paste | Fail-closed token and private-key pattern filter. |
| Provider onboarding leaks into audit | Raw values go to Windows Credential Manager; MCP stores only hashes. |
| CC Switch credential export | Public CLI list/fetch/switch only; no database or config export. |
| Secret in process/URL history | No secret CLI arguments or provider deep links. |
| Audit row edited in place | Chained event and payload SHA-256 verification. |
| Generated patch changes protected state | Draft-only storage and protected path checks. |
| Public remote listener | Remote backend rejects non-loopback binds; Funnel is prohibited. |
| Cross-tailnet-user access | Exact Tailscale login allowlist at every non-health request. |
| Forged loopback identity header | Independent 256-bit private-link credential is required on every API request; backend stores only its SHA-256. |
| Browser request forgery | Per-process CSRF token plus HTTPS same-origin validation. |
| Remote write flooding | Per-identity write limit, body cap, and socket timeout. |
| Cross-scope observation | Scope predicate is applied in SQLite before row limits. |
| Cross-room memory disclosure | Room membership and owner checks precede every memory read. |
| Model self-promotes a global belief | Only `human-operator` can create or revoke Project memory. |
| Memory source changes later | Message/artifact/parent SHA-256 is embedded in the memory row. |
| Provider receipt leaks memory | Runner receipts retain tool/result hashes, never prompts or bodies. |
| Chat attachment path leaks identity | Explicit selections are copied under SHA-256 names; original absolute paths and filenames are not stored in messages. |
| Attachment store becomes Agent scratch space | The store is protected from task write scopes; only live content-addressed files pass message binding. |
| Local attachments enter a release | `.peerbridge-artifacts` is ignored by Git and the strict release source snapshot. |

## Known limitations

1. The local SQLite database is not encrypted.
2. Secret filtering is pattern based and cannot identify every secret or personal datum.
3. A process with direct filesystem access can modify files without using PeerBridge.
4. A process with write access to the database can delete the audit tail. Without an
   external chain-head receipt, tail deletion is not independently detectable.
5. Hashes establish byte identity, not semantic correctness.
6. Peer review quality depends on the reviewing model and supplied evidence.
7. Presence proves recent database contact, not that a human is watching the agent.
8. The pixel monitor is an observability interface, not an authorization boundary.
9. Route profile names and requested model labels are not provider attestations. Only an
   observed matching session can create a route receipt, and upstream identity still
   requires an independent provider receipt when that distinction matters.
10. PeerBridge does not sandbox commands or inspect network traffic.
11. Credentials exist briefly in local process memory during save and provider use.
12. An endpoint SHA-256 hides the endpoint value but can still confirm a guessed URL.
13. A local process can forge the Tailscale identity header when calling loopback, but it
    must also obtain the private-link credential from the current-user-only ACL file or
    process memory. The trusted operating-system-account assumption remains mandatory;
    other non-administrator accounts are outside that ACL.
14. Private mobile mode is not a general remote MCP transport and cannot operate when the
    PC or Tailscale service is offline.
15. Remote/mobile release requires authenticated encrypted transport, reconnect,
    authorization, crash recovery, and human-intervention E2E evidence.
16. Explicit memory text is visible to every authorized reader of its visibility scope;
    PeerBridge cannot make a disclosed Room or Project memory secret again.
17. PeerBridge does not and cannot verify that a provider has erased text from its own
    transient inference context after an authorized read.
18. Attachment validation detects supported file signatures and obvious credential text;
    it does not semantically inspect images or guarantee that a provider supports vision.

## Deployment guidance

- Keep `.peerbridge/` on a local filesystem with account-level access control.
- Never publish database, WAL, draft, or monitor screenshots containing private content.
- Use a separate scope and database for unrelated projects.
- Add sensitive project paths with repeated `--protected-path` options.
- Export audit-chain heads to a separate append-only store for higher assurance.
- Configure MCP client approval prompts for state-changing tools.
- Run `peerbridge doctor` and project tests before release or handoff.
- Use Tailscale Serve only. Never use Funnel or expose the loopback backend through a
  public reverse proxy.
