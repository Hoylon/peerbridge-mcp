# PeerBridge Local Alpha Support Matrix

This matrix describes the `0.1.0-alpha.5` local candidate. A test or implementation entry
does not upgrade a provider route to verified official identity.

## Platforms and distribution

| Area | Alpha state | Notes |
| --- | --- | --- |
| Windows 10/11 | Supported candidate | Primary development and physical QA platform. |
| Windows x64 portable ZIP | Verified candidate | Fresh create-only extraction passes UI self-test, frozen MCP send, initialization, and `doctor`; the executable is not code-signed. |
| Python | 3.11+ | Dependency-free coordination core; encrypted feedback uses the optional `feedback` extra. |
| Linux/macOS | Untested Alpha | The stdio/SQLite core is portable, but this release has no acceptance receipt for these platforms. |
| Wheel and source distribution | Verified candidate | Strict package and extracted-artifact smoke checks pass. |
| Native Windows installer | Not included | The portable ZIP is not an installer. No signed installer, automatic updater, or rollback yet. |
| Remote/mobile | Experimental, excluded | Existing tailnet evidence is preserved but is outside the local Alpha profile. |

## Clients, providers, and models

| Integration | State | Evidence boundary |
| --- | --- | --- |
| Codex MCP client | Supported | Stdio registration documented; official visible model catalog is dynamically discovered. |
| Official Codex models | Implemented / physical reverify | Current discovery returned Sol, Terra, Luna, 5.5, 5.4, 5.4 Mini, and 5.3 Spark variants with model-specific reasoning choices. Availability remains client/account dependent. |
| Claude Code MCP client | Supported contract | Stdio registration documented; final public live multi-provider room receipt is still pending. |
| Kimi Code CLI | Compatible registration path | Kimi CLI can register the same stdio server; relay/API behavior requires a configured runner and its own receipt. |
| OpenAI-compatible relay/local endpoint | Implemented / onboarding partial | Direct routes do not require CC Switch. Secrets stay in Windows Credential Manager. Guided discovery/edit/rotation UX is not complete. |
| CC Switch import | Partial | Public-CLI discovery exists; provider-family coverage and import/refresh UX need more validation. |
| Official Grok ACP/client | Partial | Client/ACP receipt code exists; the complete current initialize-to-tool-call-and-recovery release receipt is pending. |
| DeepSeek/Gemini-compatible routes | Contract only until configured | Register as explicit relay/local routes; no provider identity is inferred from a model string or API shape. |

`official`, `relay`, and `local` are evidence classes. Relay names and model response strings
do not prove the upstream operator. PeerBridge records requested, expected-response, and
observed model identities separately and fails closed on an unconfigured mismatch.

## Desktop capabilities

| Capability | Alpha state | Limitation |
| --- | --- | --- |
| Multiple rooms and reusable Agent seats | Implemented | Context does not silently cross rooms. |
| Root-post room wakeup | Verified local Alpha | Human and Agent root-post receipts prove routed fanout; offline seats terminate rather than hang. |
| Bounded parallel discussion | Verified local Alpha | A live Grok/Kimi round reached consensus; consensus, blocker, stagnation, round, message, and timeout limits prevent loops. |
| Per-seat provider/model/reasoning | Verified local Alpha | Physical apply/restore and persistence checks pass; choices remain provider- and model-specific. |
| Drag add/remove | Verified local Alpha | Physical add/remove and deterministic drag tests pass; removal preserves history and requires confirmation. |
| Traditional Chinese / Simplified Chinese / English | Supported | Low-level engineering diagnostics may remain English in Alpha. |
| First-run tutorial | Supported | Screenshots/video are not bundled yet. |
| Read-only update check | Supported | Alpha checks include published GitHub releases; no automatic installation. |
| Image/text attachment transport | Supported with limits | Agents do not yet receive provider-specific multimodal image payloads. |
| Skills | Not implemented | No picker, version/hash binding, permission preview, or invocation provenance yet. |
| Private feedback | Verified local Alpha | The packaged HTTPS intake validates a sealed bundle, stores it temporarily in private R2 with bounded D1 metadata, and sends a maintainer notification. Normal diagnostics redact secrets; complete-key escalation is explicit and locally encrypted before upload. Local retry fallback is preserved. |
| Usage analytics | Off by default, local only | No central sender and no claim of unique-user counts. |

## Resource and data boundaries

- SQLite is authoritative for local coordination; PostgreSQL and cloud sync are not included.
- One singleton supervisor coordinates provider runners. Duplicate writers are rejected.
- Room history, parallelism, discussion budgets, and provider subprocess resources are
  bounded. The passing local soak does not cover the desktop client's own memory.
- `.peerbridge/` and `.peerbridge-artifacts/` are private runtime data and must not be
  published or attached to public reports.
- Provider credentials and complete private endpoints do not enter SQLite, MCP messages,
  normal feedback, analytics, receipts, or Git history.

## Release interpretation

This is an honest Alpha support matrix, not a Stable compatibility promise. Features marked
`physical reverify`, `live-provider reverify`, `partial`, or `contract only` remain visible
limitations until a current receipt closes the stated boundary.
