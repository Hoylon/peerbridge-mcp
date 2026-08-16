# Memory and Long-Running Operations

PeerBridge separates three kinds of memory that are easy to confuse:

1. the coding client's task history and desktop renderer;
2. PeerBridge coordination state in SQLite; and
3. short-lived provider runner processes.

PeerBridge can bound the second and third categories. It cannot force a separate coding
client to unload an unusually large task transcript from that client's own process.

## Bounded local design

The local Alpha uses these controls:

- The Control Room renders only the selected tab.
- Room history is paged instead of loading an entire room into Tk widgets.
- SQLite `PRAGMA data_version` is used as a logical change token, so an unchanged WAL
  file does not trigger needless full redraws.
- One process-level supervisor lock prevents duplicate mailbox writers.
- Provider runners are created for one claimed dispatch and then released.
- Idle supervisor cycles inspect pending root dispatches before probing provider
  credentials, so saved but unused routes do not create background provider churn.
- Runtime slots cap concurrent provider work and release automatically after a crash.
- A memory-headroom gate refuses to start optional provider work when the host is under
  pressure.
- Full-capability local read-only UI polling does not append one audit pair per tool call.
  Mutations and restricted model-session reads remain audited, preserving provider
  evidence without allowing an idle monitor to grow the audit chain indefinitely.
- Pytest scratch data uses the operating-system temporary directory rather than growing
  numbered `.pytest-tmp*` directories in the repository.

These controls reduce PeerBridge's own footprint. They are not a claim that every
supported desktop client is leak-free.

On a 16 GB host, optional model inference is deliberately blocked when available
physical memory falls below the configured minimum (currently 2 GiB and 8 percent).
Do not weaken this gate to make a test pass. A coding client holding a very long active
task can consume most of the machine even while PeerBridge's monitor and provider
workers remain small. Continue deterministic tests and documentation work, then restart
or compact that client before the real-provider gate. A 32 GB upgrade improves
headroom, but it is not a substitute for the bounded-process and soak-test gates below.

## Durable continuity without one endless task

Use one coding task for one coherent outcome. At a milestone:

1. record changed paths, tests, and live SHA-256 evidence in PeerBridge;
2. update the repository's small continuity document or handoff;
3. leave the raw task transcript intact as cold evidence;
4. continue in a focused task that reads the continuity document and live artifacts,
   not the complete historical transcript; and
5. re-read live Git, process, database, lock, and test state before writing.

This preserves history while keeping the active context small. Codex `/compact` can
reduce model context, but it does not promise to shrink the local rollout file or make a
desktop renderer unload every historical UI object.

Never copy credentials, hidden reasoning, or unrelated private material into a handoff.
Bind important artifacts by path, byte count, and SHA-256 instead.

## Windows verification command

Use the project environment and a fresh explicit temporary root. This avoids Microsoft
Store Python aliases and stale ACLs on a reused pytest directory:

```powershell
$run = Join-Path $env:TEMP ("peerbridge-pytest-" + [guid]::NewGuid())
.\.venv\Scripts\python.exe -m pytest -q --basetemp $run
```

Do not delete an old test directory merely because it is inaccessible. A unique new
directory is sufficient for verification and preserves evidence for later inspection.

Verify the current continuity bindings with:

```powershell
.\.venv\Scripts\python.exe scripts\verify_continuity_manifest.py
```

Run the isolated local Alpha memory/crash soak without provider credentials or model
requests, then verify the create-only receipt without writes:

```powershell
.\.venv\Scripts\python.exe -m peerbridge_mcp.local_alpha_soak `
  --project-root . `
  --output .peerbridge\receipts\local-alpha-soak-<date>-v1.json

.\.venv\Scripts\python.exe -m peerbridge_mcp.local_alpha_soak `
  --project-root . `
  --verify .peerbridge\receipts\local-alpha-soak-<date>-v1.json
```

The harness grows an isolated room history, repeatedly reads bounded monitor pages,
checks a resident-memory plateau, kills only its own helper process, proves that the
supervisor lock and provider-runtime slot are released, reopens the database, and binds
the result to the exact implementation SHA-256 values. It never uses provider credentials
or the live coordination database.

Capture a small live resume snapshot without embedding raw chat history or message
bodies:

```powershell
.\.venv\Scripts\peerbridge-continuity.exe capture `
  --project-root . `
  --db .peerbridge\peerbridge.sqlite3 `
  --scope peerbridge-main

.\.venv\Scripts\peerbridge-continuity.exe verify `
  --project-root .
```

The default output is `.peerbridge/continuity/current.json`. It stores bounded table
counts, high-water marks, task/room/route identities, recent receipt hashes, and Git
path state. It deliberately does not copy message bodies, raw coding-client history,
or credentials. A resumed task must still recompute live Git, process, lock, database,
and test state before writing.

Monitor list queries use scope-and-time indexes so an active 100+ MB coordination
database does not repeatedly build temporary sort trees on every refresh.

## Read-only diagnosis

```powershell
Get-Process codex,ChatGPT,python,pythonw -ErrorAction SilentlyContinue |
  Sort-Object WorkingSet64 -Descending |
  Select-Object Id,ProcessName,
    @{N='WS_MiB';E={[math]::Round($_.WorkingSet64/1MB,1)}},
    @{N='Private_MiB';E={[math]::Round($_.PrivateMemorySize64/1MB,1)}}

Get-Item -LiteralPath <exact-rollout-jsonl-path> |
  Select-Object FullName,Length,LastWriteTimeUtc
```

Measure first. Do not terminate processes based only on a name: bind an owned PID,
start time, command line, and expected role before changing lifecycle state.

## Release gate

The local Alpha is not memory-ready for publication until a bounded soak test proves:

- no duplicate supervisor;
- no abandoned provider runtime slots;
- room paging remains bounded as message count grows;
- crash and restart preserve messages and audit continuity; and
- resident memory reaches an explainable plateau during the test window.
