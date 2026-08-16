# Security Policy

## Supported versions

PeerBridge MCP is pre-1.0. Security fixes are applied to the latest tagged release and
the default branch.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository. If that feature is not
available, open a minimal issue asking the maintainer for a private contact channel. Do
not include exploit details, credentials, private paths, database contents, or user data
in a public issue.

Please include:

- affected version and operating system;
- whether the issue crosses the configured project root;
- the smallest safe reproduction;
- impact on confidentiality, integrity, or availability;
- any suggested mitigation.

The maintainer will acknowledge a complete report as soon as practical. A response time
is not guaranteed for this volunteer-maintained alpha project.

## Security expectations

- Treat every MCP client and local process as untrusted until configured.
- Use precise task path scopes. Avoid claiming the project root as a write scope.
- Do not place credentials in messages, reviews, drafts, or proof text.
- Keep `.peerbridge/` private and out of version control.
- Keep `.peerbridge-artifacts/` private and out of version control. Chat attachments are
  explicit project data even though their stored filenames are content hashes.
- Use operating-system permissions to restrict the project and SQLite files.
- Verify package hashes and audit-chain heads before relying on archived evidence.
- Keep human approval enabled for tools that mutate coordination state.
- Treat room membership as an authorization boundary. Do not copy hidden context between
  rooms; use an explicit audited summary message when cross-room sharing is intended.
- Treat the Memory Ledger as explicit project data, not model-private memory. Never store
  chain-of-thought, credentials, private endpoints, or unrelated personal data in it.
- Only the human operator may promote or revoke Project memory. Keep model runners on the
  default read-only memory allowlist unless a narrower audited workflow requires otherwise.
- Keep the remote backend on loopback and place it only behind authenticated Tailscale
  Serve. Never publish it with Funnel or a public reverse proxy.
- Select chat attachments explicitly. PeerBridge rejects unsupported types, oversized
  files, symlinks, mismatched file signatures, and obvious credentials in text, but it
  cannot determine whether every image or document contains private visual information.

## Provider credential promise

PeerBridge separates the **secret plane** from the **coordination plane**.

- A provider API key and its complete private endpoint are stored only in Windows
  Credential Manager, or remain under CC Switch when the operator imports an existing
  CC Switch provider.
- MCP tools, SQLite rows, messages, events, receipts, logs, command-line arguments,
  deep links, Git history, and telemetry receive no raw key or complete private endpoint.
- Provider records may contain non-secret IDs, backend type, enabled state, and an
  opaque random credential version. That version is generated independently of the
  secret; PeerBridge must not persist a secret-derived fingerprint or checksum.
- PeerBridge uses the public CC Switch CLI only for provider listing, model discovery,
  and an explicitly confirmed switch. It does not read the CC Switch database, export
  configuration, or silently change the active provider.
- PeerBridge has no credential telemetry. Normal operation and normal feedback never
  upload a provider secret to the PeerBridge maintainers.
- The Feedback page has one explicit, opt-in escalation for parser/import failures. The
  operator may paste the complete credential once and tick the encryption checkbox. The
  plaintext then exists only in process memory, is encrypted locally with AES-256-GCM,
  and its one-time data key is wrapped to a release-bound RSA-3072-or-stronger support
  public key using OAEP-SHA256. The support key file is pinned by SHA-256 in the signed
  release support configuration and is rechecked immediately before encryption.
- Only the encrypted credential envelope enters the feedback ZIP. The plaintext key and
  any secret-derived hash, prefix, or fingerprint never enter the report, attachment
  metadata, logs, SQLite, audit events, analytics, HTTP headers, or email body. The UI
  clears the credential field after the attempt, whether delivery succeeds or fails.
- Screenshots and diagnostic attachments are **not encrypted by that credential
  envelope**. They are included only after explicit file selection, are snapshotted once,
  content-checked, bounded by type/count/size, and listed before submission. Users must
  remove unrelated private content before attaching a file.
- Feedback recipient identity, HTTPS endpoint, privacy URL, and support public-key
  fingerprint come from the package-bound
  `src/peerbridge_mcp/release_support/support.json`; a source checkout may use the explicit
  maintainer-development override `support/support.json`. Mutable user profiles, room
  messages, providers, and command-line arguments cannot redirect feedback. If no private
  endpoint or mail route is configured, PeerBridge saves a local bundle and case code
  instead of sending it.

Secrets necessarily exist briefly in the local process memory while the operator saves
or an adapter uses them. A compromised local account, memory debugger, or administrator
is outside this protection boundary. Rotate any key exposed in a screenshot, chat,
shell history, or public issue before using this feature.

PeerBridge prevents neither a malicious local administrator nor a compromised agent from
editing files outside the bridge. See [docs/threat-model.md](docs/threat-model.md).

## Pre-release provider execution hardening

The controls in this section are required design targets, not a statement that a
published build already implements them. Documentation, configuration, and unit tests
alone are not release evidence. See the detailed
[provider routing contract](docs/provider-routing.md).

- Credential descriptors must bind the exact route evidence class, provider, adapter,
  endpoint record, slot, target, schema, and opaque random credential version. A mismatch
  or ambiguous lookup fails closed.
- New secrets use collision-free v2 targets based on uniqueness-constrained stable IDs.
  Legacy records are read only for verified, idempotent migration to v2; migration never
  overwrites another target or silently deletes the legacy record.
- Credential versions are random opaque identifiers, never hashes or fragments of the
  secret. No secret-derived fingerprint belongs in receipts, logs, events, or SQLite.
- Provider HTTP clients disable redirects. A `3xx` response is terminal, and credentials
  are never forwarded to a redirected origin.
- Provider processes run from a controlled canonical working directory and do not inherit
  an arbitrary caller `cwd` or resolve security-sensitive files through relative paths.
- Each operation is classified for idempotency before dispatch. Only bounded transient,
  replay-safe failures may retry; ambiguous mutations and deterministic policy or client
  failures do not retry automatically.
- Each model operation has finite cumulative MCP call, result-size/token, nesting,
  round-trip, retry, and elapsed-time budgets. Retries and failed calls consume the same
  budget, and exhaustion fails closed with typed, auditable status.

The labels `official`, `relay`, and `local` are evidence classes. They describe what the
run can prove about its route and transport; they must not overclaim the identity of an
upstream provider or model. Compatible APIs, configured names, and self-reported model
strings are claims unless separately attested and recorded as such.

## Private mobile boundary

Private mobile mode accepts one mutation: an explicit human `send_message` call. It uses
the same stdio MCP validator and audit chain as the desktop composer. The web process has
no shell, patch, task-claim, approval, credential, or arbitrary-file endpoint.

Tailscale authenticates the remote user. PeerBridge authorizes an exact login allowlist,
but stores only a SHA-derived operator identity. Since the local backend trusts the proxy
header, any process under the same Windows account can impersonate that remote identity.
The local account must therefore remain trusted and free of untrusted workloads.
