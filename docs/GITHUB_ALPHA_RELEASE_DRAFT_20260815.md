# GitHub Pre-release Draft: PeerBridge MCP v0.1.0-alpha.1

Use this text only after the frozen artifacts pass every gate and the operator authorizes
publication. Mark the GitHub release as
**Pre-release**. Do not mark it Latest or Stable.

## PeerBridge MCP v0.1.0-alpha.1

PeerBridge is a local-first, auditable coordination layer for equal AI coding peers and their
human operator. This Alpha provides a shared SQLite mailbox, scoped writer leases, room-based
Agent collaboration, bounded parallel discussion, review/proof records, durable memory with
provenance, and a pixel-style local control room.

### What is included

- Local stdio MCP coordination with no required runtime dependencies beyond Python 3.11+.
- Persistent rooms and reusable Agent seats.
- Human or Agent root-post fanout with bounded discussion and no reply cascades.
- Per-seat provider, model, and reasoning selection with dynamic model catalogs.
- Official Codex catalog discovery, including the currently available Sol, Terra, and Luna
  variants, without hard-coded runtime model lists.
- Traditional Chinese, Simplified Chinese, and English desktop UI.
- First-run tutorial, read-only update check, safe local image/text attachments, and private
  provider-independent feedback with optional public-key encryption.
- Default-off, local-only aggregate analytics. This Alpha has no analytics sender.
- Bounded memory/resource controls and crash-recovery gates.

### Install from the release asset

**Windows portable app:** download
`PeerBridgeControlRoom-0.1.0a1-windows-x64-portable.zip`, verify its published SHA-256,
extract the complete archive, and run `Launch PeerBridge.cmd`. The unsigned Alpha may
trigger Windows SmartScreen. It stores local state under
`%LOCALAPPDATA%\PeerBridge\workspace` and includes no provider credentials.

**Python wheel:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .\peerbridge_mcp-0.1.0a1-py3-none-any.whl
peerbridge init --project-root . --scope demo
peerbridge doctor --project-root . --scope demo
peerbridge-monitor --project-root . --scope demo
```

### Verified candidate

- Automated tests: **509 collected, 508 passed, 1 intentionally skipped, 0 failed**.
- Memory soak: 1,200 messages with 131,072 bytes of private-memory plateau growth in the final
  four samples, below the 24 MiB acceptance limit; zero-write receipt verification passed.
- Continuity manifest: verify the final frozen manifest with zero writes before publication.
- Wheel and source-distribution byte counts and SHA-256 values: copy them from the local
  create-only `.peerbridge/receipts/local-alpha-release-final-v3.json` generated after the
  tracked release source is frozen. Do not copy hashes from an earlier candidate directory.

### Alpha limitations

- The public API and SQLite schema may change before 1.0.
- A signed native Windows installer and signed one-click updater are not included.
- The portable Windows executable is unsigned and is distributed as an extract-and-run ZIP.
- Experimental self-hosted remote/mobile code ships in the source distribution but is
  default-off, unsupported, and outside the local Alpha evidence/support boundary.
- Managed cloud sync, paid remote service, native iPhone, and mobile UI redesign are not included.
- Real multi-provider behavior depends on the user's own provider accounts, credentials, limits,
  and model availability. This release does not include provider credentials.
- Physical disposable send/remove/route acceptance was completed on Windows. Any later change
  to drag geometry, routing persistence, or attachment transport requires that acceptance to
  be repeated before publishing.

### Privacy and support

Provider credentials remain in the operating-system credential store and do not enter the
project database, MCP messages, normal feedback, analytics, or Git history. Analytics is off by
default and has no sender in this Alpha. Do not attach private databases, transcripts, API keys,
or provider configuration to public GitHub issues. Follow `SECURITY.md` for private reporting.

Before installing or reporting a problem, review:

- [Alpha support matrix](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.1/docs/alpha-support-matrix.md)
- [Alpha troubleshooting](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.1/docs/alpha-troubleshooting.md)
- [繁體中文快速開始](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.1/docs/alpha-quickstart.zh-Hant.md)
- [简体中文快速開始](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.1/docs/alpha-quickstart.zh-Hans.md)

Source-code license: Apache-2.0. The PeerBridge name and logo are governed separately
by `TRADEMARKS.md` and `BRAND_ASSETS.md`; those notices are included in every artifact.
