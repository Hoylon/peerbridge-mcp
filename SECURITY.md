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
- Use operating-system permissions to restrict the project and SQLite files.
- Verify package hashes and audit-chain heads before relying on archived evidence.
- Keep human approval enabled for tools that mutate coordination state.

PeerBridge prevents neither a malicious local administrator nor a compromised agent from
editing files outside the bridge. See [docs/threat-model.md](docs/threat-model.md).
