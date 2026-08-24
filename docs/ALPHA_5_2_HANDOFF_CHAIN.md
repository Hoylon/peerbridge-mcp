# PeerBridge Alpha 5.2 Safety And Handoff Chain

This is the recovery contract for `v0.1.0-alpha.5.2`. It contains no session transcript,
hidden reasoning, credential, or release-success claim.

## Recovery order

1. Read `docs/ALPHA_5_2_REQUIREMENTS.md` and `ROADMAP.md`.
2. Recompute live `git status`, `HEAD`, branch, processes, database, and relevant tests.
3. Locate the latest create-only checkpoint under
   `.peerbridge-artifacts/handoff/alpha52/` and verify its complete chain.
4. Verify every authority, backup, and evidence SHA named by that checkpoint.
5. Continue only the checkpoint's `next_phase`; do not infer completion from a handoff.
6. Before another phase boundary, create a new checkpoint linked to the previous file.

The checkpoint is evidence about one observed state. Later edits are expected to change the
live worktree and do not invalidate the historical checkpoint itself.

## Immutable recovery points

- Published Alpha 5.1 source commit:
  `717ad4448746404de30ac582c6507bcd2ba60bd5`.
- Published annotated tag: `v0.1.0-alpha.5.1`.
- Pre-implementation authority bundle:
  `.peerbridge-artifacts/backups/alpha52-authority-20260819T044410Z/peerbridge-alpha52-authority.bundle`.
- Bundle SHA-256:
  `a1c173372301af667d1055693c8dfb32c8cd9657512ac335dbf8cd900beed594`.
- Authority snapshot commit stored in that bundle:
  `6fc5c3c71e58197c4745cc088dfcd76b27baed13`.

The bundle was verified as complete and contains `main`, the Alpha 5.1 annotated tag, and
the authority snapshot. Existing OSS application drafts, `tmp/`, prior releases, and prior
artifacts are intentionally preserved in place and are not release inputs for 5.2.

## Checkpoint command

Use a new output name every time. Never overwrite an earlier checkpoint.

```powershell
python scripts/capture_release_handoff.py capture `
  --project-root . `
  --release v0.1.0-alpha.5.2 `
  --package-version 0.1.0a5.post2 `
  --phase <current-phase> `
  --next-phase <next-phase> `
  --previous <previous-checkpoint-if-any> `
  --backup .peerbridge-artifacts/backups/alpha52-authority-20260819T044410Z/peerbridge-alpha52-authority.bundle `
  --output .peerbridge-artifacts/handoff/alpha52/<unique-name>.json
```

Verify the complete chain without writing:

```powershell
python scripts/capture_release_handoff.py verify `
  --project-root . `
  --checkpoint .peerbridge-artifacts/handoff/alpha52/<latest-name>.json
```

## Safety invariants

- Existing user changes and untracked artifacts are preserved.
- Historical release artifacts are never overwritten, moved, or deleted.
- Every release artifact is built from the final annotated tag, not from an earlier dirty
  worktree.
- A passing test or checkpoint never means the GitHub Release exists.
- Cloud collaboration remains disabled in 5.2; existing announcement and feedback services
  are separate data planes.
- Submission of external forms is never part of this release chain.
