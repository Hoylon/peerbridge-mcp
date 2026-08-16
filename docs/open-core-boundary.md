# Open-Core and Commercial Boundary

PeerBridge should earn trust through a useful, non-crippled local core. Commercial value
should come from operating optional services, not from hiding the protocol, security
rules, or a user's own audit data.

This document is product architecture, not a promise that hosted services already
exist.

## Decision summary

- Do not obfuscate, minify for secrecy, or deliberately scramble the public core.
  Those techniques weaken reviewability without creating a durable commercial moat.
- Keep protocol behavior, local coordination, security gates, audit verification,
  provider adapter contracts, and the complete local Control Room usable in public.
- Put future commercial value behind clean service boundaries: managed uptime,
  encrypted cross-device sync, enterprise governance, hosted delivery, fleet
  analytics, support, and optional domain services.
- Never make secret collection, private conversation retention, or a hidden routing
  algorithm a condition of using the local core.

## Open local core

The following belongs in the public repository:

- MCP protocol implementation and provider-neutral client contracts;
- SQLite coordination, rooms, messages, leases, reviews, proofs, and memory ledger;
- audit-chain generation and independent verification;
- the human Control Room and local human-intervention flow;
- provider route descriptors and secret-store interfaces;
- local supervisor, resource guard, retries, cancellation, and crash recovery;
- self-hosted authenticated remote-access building blocks;
- import, export, migrations, tests, threat model, and clean-install instructions; and
- enough adapters and fixtures to prove interoperability without paid credentials.

Users must be able to inspect where a request went, what model identity was observed,
what files were in scope, and what evidence supported completion. Essential local
security and audit verification must not become a paid-only feature.

## Optional closed services

Future proprietary services should be separately deployable components with explicit
interfaces:

| Service | Commercial value |
| --- | --- |
| Managed runner control plane | 24/7 lifecycle, autoscaling, upgrades, and incident response |
| Managed encrypted sync | Cross-device state, backups, retention, and recovery |
| Enterprise governance | SSO, SCIM, organization policy, approval workflows, and SLA |
| Hosted mobile delivery | Push notifications and managed remote intervention |
| Billing and quota service | Metering, budgets, invoices, and abuse controls |
| Hosted evaluation analytics | Fleet-level routing quality, reliability, and cost analysis |
| Proprietary domain packs | Optional specialized policies, eval sets, and workflows |

The hosted layer must never require users to surrender provider keys to an unaudited
message log. Credentials remain in an operating-system secret store or a documented
managed secret boundary, and telemetry must exclude secret values by construction.

## License reality

PeerBridge is currently Apache-2.0. Code released under that license remains available
under that license; it cannot later be made retroactively closed. A sustainable model is
therefore:

1. keep the local coordination and audit core Apache-2.0;
2. develop hosted operations as separate services or repositories;
3. keep stable open interfaces between local and hosted components;
4. publish versioned releases and migrations instead of rewriting history; and
5. maintain `TRADEMARKS.md` and `BRAND_ASSETS.md`; obtain jurisdiction-specific
   legal review before registration or any material policy change.

Do not change the current license merely to create artificial scarcity.

## Independent Agent review record

The product boundary was submitted through PeerBridge itself as a bounded, low-memory
discussion. This record distinguishes external advice from the operator's decision.

| Field | Verified value |
| --- | --- |
| Discussion | `bcd8c45b09904d8982afc421b209deb2` |
| Task | `open-core-commercial-boundary-v3-20260814` |
| Prompt SHA-256 | `78fef18565f521144fc52fd0ce524bea328116cec5e82d16af7dde1b0a74eabc` |
| Responding Agent | `grok-relay` |
| Observed provider / model | `relay-grok-sui-xiang` / `grok-4.6` |
| Route verification receipt | `38a0abd787d56d082690907f359df68e846bd3916aae9334eba1ffe81a14288c` |
| Inference receipt | `c5d538b2294ebcdefbb8a1a39fe92db7f64e1ea9fd7188e14363f885620c8be5` |
| Response message / SHA-256 | `1f12de968a5c494c930c09fd21189e79` / `f21e839df4dd90fd96dc1dd5cf1570bbca172dffb5a94e0f8d7831d281d44486` |

Grok recommended an inspectable local coordination and audit core, separately hosted
enterprise services, and no code obfuscation. It also recommended a documented threat
model, integration tests, release history, and transparent data handling before public
release. These points informed, but did not automatically determine, the policy above.

The same round addressed `kimi-relay`, but that dispatch terminated with
`tool_policy_failed` after two resource-pressure retries followed by a third attempt.
No Kimi opinion was obtained and no two-Agent consensus is claimed. The durable retry
schedule correctly spaced the resource retries instead of exhausting all attempts in a
tight loop. A later Kimi review may be appended when the provider/tool compatibility
issue is fixed and the host is above the memory safety threshold.

This evidence is stored in the local SQLite audit database. It contains no provider
credential value and does not make the external Agent a product authority.

## Publication and program applications

An application for an external open-source program should be based on verifiable work:

- a clean install from a fresh clone;
- a short reproducible multi-agent demonstration;
- real tests for identity, authorization, crash recovery, and audit verification;
- a public threat model and honest limitations;
- visible release history, issues, and maintenance; and
- a clear distinction between implemented behavior and roadmap items.

No six-month ChatGPT Pro benefit is guaranteed. Eligibility and current application
terms must be checked against official OpenAI material at submission time. PeerBridge
must not invent affiliation, usage numbers, security certification, or provider support.
