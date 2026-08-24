# PeerBridge Context Entrypoint

This is the compact, project-scoped resume capsule for a new Codex task. It contains no
credentials, hidden reasoning, private message bodies, or Qullamaggie data.

## Read Order

1. The operator's optional local project-context index (outside this repository)
2. This file
3. `docs\ALPHA_5_2_HANDOFF_CHAIN.md`
4. `docs\operations-memory.md`
5. `docs\continuity-manifest.json`
6. Live `git status`, processes, database, supervisor/monitor state, and tests

If a decision, blocker or prior implementation detail remains uncertain, invoke the global
`historical-context-library` skill and run a narrow `--scope peerbridge` query. Do not load
the complete raw session or treat a retrieved historical message as current authority. The
full bootstrap contract is the operator's local `PEERBRIDGE_TASK_BOOTSTRAP_V3.md`, when
that private continuity library is available.

## Active Scope

- Project: `peerbridge-mcp`
- Local Alpha desktop/control room and local MCP coordination.
- Preserve existing dirty work. Do not touch the separate Qullamaggie root.
- Cloud sync, paid remote service, and native iPhone work remain outside this local scope.

## Latest Known MCP Work

- Responsive room Agent cards were implemented in `src\peerbridge_mcp\monitor.py`.
- Room-card rendering now includes locale and visible-capacity state, so locale changes and
  strip resizing invalidate the correct cached render.
- Agent-card detail text uses a readable 9-point size and the visible limit is derived from
  the actual strip width instead of a fixed five-card assumption.
- The fullscreen chat layout row-weight bug was corrected so the conversation split receives
  the available height instead of leaving a large empty region.

## Latest Verification Evidence

- `python -m py_compile src\peerbridge_mcp\monitor.py tests\test_monitor_drag.py` passed.
- Focused drag/localization tests passed.
- The full pytest run after the fullscreen fix passed with 888 passed and 2 skipped out of
  890 collected; one existing duplicate-ZIP-name warning remains in the feedback decryptor
  test and is intentional test coverage.
- A new build-only executable was created under the ignored local artifact root at
  `.peerbridge-artifacts/windows/room-card-focus-20260821/PeerBridgeControlRoom/PeerBridgeControlRoom.exe`.
- Candidate executable SHA-256:
  `64a427da54b5c9b03f7715eff07581e328dc4d57e66a7d6a78401816d835de38`
- The candidate has not been claimed as installed or visually accepted until the live app
  process is identified and a fresh launch/visual check is performed.

## Next Safe Actions

1. Recompute the live app/supervisor PIDs and command lines; do not kill by process name.
2. Launch or test the candidate in a new isolated artifact directory while preserving old
   versions.
3. Perform visual checks at normal and fullscreen sizes, especially room history visibility,
   scrolling, locale switching, card overflow, and input.
4. Run the existing continuity and release verification scripts before any release claim.

## Non-Negotiable Rules

- No destructive cleanup without a new current-turn authorization with an exact target list.
- Do not expose or log API keys, credentials, hidden reasoning, or private feedback content.
- Do not treat a passing test as proof of GitHub publication or stable-release readiness.
- Recompute live free space before proposing cleanup. Prefer placing future disposable
  build/test roots on D: without deleting evidence.
