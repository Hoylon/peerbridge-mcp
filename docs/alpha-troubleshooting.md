# PeerBridge Alpha Troubleshooting

This guide covers the local Windows Alpha. Remote/mobile control, managed cloud sync,
native iPhone support, signed installers, and automatic updates are outside this release.

## Start with a safe diagnosis

From the repository root:

```powershell
.\.venv\Scripts\Activate.ps1
peerbridge doctor --project-root . --scope demo
peerbridge monitor --project-root . --scope demo
```

`doctor` opens an existing SQLite database with `mode=ro` and `query_only`; it does not
create directories or migrate schemas. A missing database produces `peerbridge init`
guidance, while an old schema produces an explicit `peerbridge migrate` command. Those
two commands are writers. Do not attach `.peerbridge/`, provider configuration, private
transcripts, or credential-store exports to a public issue.

## The monitor does not open

1. Confirm Python 3.11 or newer is active: `python --version`.
2. Confirm the package is installed in the current environment:
   `python -m pip show peerbridge-mcp`.
3. Run `peerbridge doctor --project-root . --scope demo` and keep only its redacted
   output.
4. Check whether another monitor is already open. PeerBridge rejects duplicate singleton
   runtimes instead of silently starting a competing writer.

## An Agent seat stays offline or a round stops

An offline seat is expected to terminate with durable unavailable/timeout evidence. It
must not keep a discussion open forever. Check the room stop reason and the seat route:

- the seat has a saved provider, model, and reasoning mode;
- the corresponding MCP client or provider adapter is actually running;
- the observed provider/model identity matches the requested route; and
- the room has not reached its round, message, stagnation, blocker, or timeout limit.

Use **Continue** only after fixing the unavailable route. Starting a new root message
closes an older open discussion while preserving its history.

## A model is missing from a menu

Model lists are discovered per provider. Refresh the provider catalog, then reopen the
seat card menu. For official Codex, PeerBridge reads the installed Codex client's visible
catalog without reading credentials. Relay and local catalogs depend on their configured
endpoint and may differ from official catalogs.

Do not substitute one model name for another. A relay that returns a stable deployment
alias needs an explicit expected-response-model binding; otherwise the identity check
fails closed.

## Drag add or remove appears not to work

- Drag a Global Agent Library card onto a room seat area to add a room-scoped seat.
- Drag a room seat left onto the full Global Agent Library removal target to remove it
  from that room.
- Removal requires a history-preserving confirmation. It does not delete the global Agent
  identity, another room's seat, or prior messages.
- Windows display scaling can change hit targets. If the confirmation does not appear,
  use the explicit **Add seat** or **Remove** control and report the scaling percentage.

## An attachment is rejected

The Alpha accepts explicitly selected PNG, JPEG, GIF, WebP, UTF-8 text, Markdown, CSV,
JSON, and log files. Limits are five files, 8 MiB each, and 16 MiB total. Symlinks,
signature/type mismatches, unsupported files, and credential-like text are rejected.

An accepted attachment is transported as an auditable local artifact reference. It does
not prove that the selected model can understand images; multimodal provider payloads are
not part of this Alpha.

## Provider or API-key onboarding fails

Use **Feedback** rather than a public issue. Normal diagnostics redact secrets. For a
parser/import failure, the user may explicitly opt in once to paste the complete key;
PeerBridge encrypts it locally to the release-bound maintainer support public key and
clears plaintext after the attempt. If private delivery is not configured or unavailable,
the app saves an encrypted local case bundle and case code.

Never paste a key into chat, screenshots, logs, GitHub issues, or command-line arguments.
Rotate any key already exposed through one of those channels.

## Memory use grows

PeerBridge bounds rendered history, room rounds, supervisor parallelism, and provider
process resources. Use the monitor diagnostics to identify the PeerBridge process first.
The Codex or Claude desktop application's memory is a separate process boundary and cannot
be controlled by PeerBridge.

Before reporting a leak, note the PeerBridge version, Windows version, room count, active
seat count, message count, provider route class, and whether memory falls after a bounded
round reaches a terminal state. Do not include prompts or credentials.

## Updates

**Check for updates** only reads GitHub release metadata. The Alpha does not install,
replace, or roll back files automatically. Download a newer pre-release deliberately,
verify its published SHA-256, and install it into a separate virtual environment until a
signed updater exists.

## Reporting safely

Use GitHub private vulnerability reporting for security issues. Use the in-app private
Feedback entry for onboarding/parser diagnostics. A public issue should contain only the
smallest redacted reproduction, version, operating system, expected behavior, and observed
error category.
