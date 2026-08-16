# Release Support Configuration

PeerBridge's Feedback page is provider-independent. A packaged public release binds it to a
maintainer-owned private HTTPS intake or support email through
`src/peerbridge_mcp/release_support/support.json`. Normal execution never reads a
project-root `support/support.json`; an untrusted checkout therefore cannot redirect
feedback or replace the support public key.
The application never accepts an endpoint or recipient supplied by an Agent, provider,
room message, mutable user profile, or command-line argument.

## Files in a configured release

- `src/peerbridge_mcp/release_support/support.json`: packaged release recipient,
  endpoint/mail route, privacy URL, and pinned public-key SHA-256.
- `src/peerbridge_mcp/release_support/peerbridge-support-public.pub`:
  RSA-3072-or-stronger **public** key used only for the user's explicit full-credential
  escalation.
- `support/support.example.json`: staging template only. It is never discovered at runtime.

For maintainer testing, start from `support.example.json` and pass its exact path only to
the explicit `FeedbackConfig.load_from_file(...)` test/tooling API. Do not add implicit
project discovery. Before packaging, copy the independently verified public configuration
and public key into `src/peerbridge_mcp/release_support/`; the release checker validates that
package-bound identity.

## Private key boundary

Generate the key pair using a trusted offline key-management workflow. Keep the private key
outside this repository, outside release artifacts, and outside `.peerbridge/`. Protect it
with an operating-system credential/key store or encrypted removable backup. Commit and
package only the public PEM.

Never paste the private key, a provider credential, or a private endpoint token into chat,
MCP messages, CI variables printed to logs, GitHub issues, tests, or screenshots.

For the Windows maintainer workstation, `scripts/setup_feedback_identity.ps1` creates one
RSA-3072 identity, stores only a DPAPI CurrentUser-protected private key under LocalAppData,
and writes the public key create-only. `scripts/decrypt_feedback_bundle.ps1` verifies a
bundle without revealing its credential by default; `-RevealCredential` is an explicit
local maintainer action that writes a create-only ACL-restricted file outside the repository.

## Configuration fields

| Field | Requirement |
| --- | --- |
| `schema` | Exactly `peerbridge.feedback-config.v1`. |
| `recipient_label` | Public display label for the maintainer-controlled recipient. |
| `endpoint` | Optional absolute public HTTPS URL with no user-info, query, or fragment. |
| `endpoint_transport` | `raw-zip-v1`, `json-base64-v1`, or the narrowly allowlisted `google-apps-script-v1`. Cloudflare intake uses `json-base64-v1`. |
| `support_email` | Optional support mailbox. Mail delivery opens a draft and leaves the encrypted bundle local for explicit attachment. |
| `privacy_url` | Optional absolute public HTTPS privacy notice. |
| `public_key_path` | Relative path beside the selected `support.json` to the public PEM. |
| `public_key_sha256` | SHA-256 of the exact PEM file bytes, in lowercase hexadecimal. |

At least one of `endpoint` or `support_email` should be configured for a public Alpha.
Full-key escalation additionally requires the pinned public key and the optional
`peerbridge-mcp[feedback]` dependency. The Windows portable artifact bundles and tests this
dependency; a wheel installation must install the `feedback` extra or `cryptography>=43`.

## Pre-release verification

1. Verify the private destination is controlled by the maintainer and refuses redirects.
2. Recompute the public PEM SHA-256 and compare it with the packaged
   `src/peerbridge_mcp/release_support/support.json`.
3. Submit a synthetic, non-secret parser failure through the packaged Feedback page.
4. Confirm the received case ID and bundle SHA match the local result.
5. Decrypt only the synthetic envelope on the maintainer side.
6. Re-run the strict release, secret/path/privacy, and extracted-artifact gates.

If these checks cannot be completed, omit the packaged `support.json`. PeerBridge then
creates a local redacted/encrypted case bundle and case code without claiming private
delivery.
