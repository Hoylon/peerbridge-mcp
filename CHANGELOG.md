# Changelog

All notable changes are documented here. The format follows Keep a Changelog and the
project uses Semantic Versioning after the initial alpha series.

## [Unreleased]

### Added

- Runtime identity labels for MCP client, provider route, and selected model.

### Fixed

- Apply protected/sensitive path checks to hash-only artifact reads.

## [0.1.0] - 2026-08-11

### Added

- Dependency-free local stdio MCP server backed by SQLite WAL.
- Per-consumer mailbox cursors and SHA-bound receipts.
- Expiring task leases with path conflict detection.
- Presence-aware, solo, and required-peer approval policies.
- Peer review requests, proof records, live file rehash, and completion gate.
- Append-only per-scope event hash chain and verifier.
- Human-operable pixel control room.
- Codex and Claude Code configuration examples.
- Legacy and MCP `2026-07-28` dual-era transport support.
- Arbitrary agent identities, configurable N-peer review quorum, and multi-agent monitor tiles.
- Additive schema v1/v2-to-v3 migration and subprocess stdio interoperability tests.

[Unreleased]: https://github.com/oscarho200407-hue/peerbridge-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/oscarho200407-hue/peerbridge-mcp/releases/tag/v0.1.0
