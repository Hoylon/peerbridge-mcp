# Threat Model

## Assets

- Project files referenced by messages, tasks, reviews, and proofs.
- Coordination metadata in `.peerbridge/peerbridge.sqlite3` and its WAL files.
- Task lease capability tokens returned to clients.
- Audit-chain integrity and externally recorded chain heads.
- Human decisions and peer review findings.

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
| Audit row edited in place | Chained event and payload SHA-256 verification. |
| Generated patch changes protected state | Draft-only storage and protected path checks. |

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
9. PeerBridge does not sandbox commands or inspect network traffic.

## Deployment guidance

- Keep `.peerbridge/` on a local filesystem with account-level access control.
- Never publish database, WAL, draft, or monitor screenshots containing private content.
- Use a separate scope and database for unrelated projects.
- Add sensitive project paths with repeated `--protected-path` options.
- Export audit-chain heads to a separate append-only store for higher assurance.
- Configure MCP client approval prompts for state-changing tools.
- Run `peerbridge doctor` and project tests before release or handoff.
