# Interface and remote research sources

This release uses four user-supplied product references. The mapping below separates
source code reuse from behavioral inspiration.

## AICSS agent conversation components

The user-supplied X post points to AICSS. Its MIT-licensed public tree contains ten React
component families: AI input, approval card, code block, data table, orbs, streaming text,
task list, text response, thinking state, and thinking plus reasoning. The website also
previews file diff, image generation, inline citations, and comparison table components;
those implementations are not present in the public tree. The AICSS website itself is
private, and its Pro components require a separate license.

PeerBridge does not add a React runtime or copy private/Pro assets. Its shared
HTML/CSS/JavaScript Workbench independently implements the relevant interaction contract:
observable Agent activity has explicit running/completed/waiting/failed states; operation
and session events have expandable evidence; chat safely renders code, links, lists, and
tables without `innerHTML`; in-turn file changes disappear from chat at the task terminal
boundary and remain in the permanent Work ledger; and the integrated diff, task, approval,
usage-chart, and cancellation surfaces use the same lifecycle on local WebView2, remote
desktop browsers, and phones. Hidden model reasoning is never fabricated or exposed.

- <https://x.com/guillaume_rygn/status/2094682322805158178>
- <https://www.aicss.dev/>
- <https://github.com/kvnkld/aicss>
- <https://github.com/kvnkld/aicss/blob/main/LICENSE>

## Todos.dev tool activity and interruption

Public product and MCP documentation describes a single workspace for tasks, Agents,
streaming build progress, cancellation, review, and lifecycle operations. PeerBridge uses
that behavior as a reference for its consistent dispatch/tool lifecycle and its real
pause/resume/continue/stop controls.

No verified public Todos.dev source repository was found, and no Todos.dev code or assets
are copied into PeerBridge. The relevant PeerBridge implementation remains local and
audited through its own `message_dispatches`, managed-session, and `control_discussion`
paths.

- <https://todos.dev/>
- <https://todos.dev/docs/mcp>

## Agent-readable DESIGN.md

The repository-root `DESIGN.md` follows the agent-readable design-contract pattern
popularized by the MIT-licensed VoltAgent collection. PeerBridge's actual tokens,
responsive rules, product shape, and security boundaries are original and tailored to an
operational multi-agent control room.

- <https://github.com/VoltAgent/awesome-design-md>
- <https://github.com/VoltAgent/awesome-design-md/blob/main/LICENSE>

## Tailcat remote toolkit

The Tailcat surface is visible and enabled by default. PeerBridge downloads a pinned
official Windows archive on first local launch, verifies both archive and executable
SHA-256, and owns one allow-listed Port/SSH/Exit-node process. The same launcher retains
separate foreground modes for port forwarding, authenticated SSH proxying, file transfer,
SOCKS5 commands, and exit-node use.

- <https://github.com/tailscale/tailcat>
- <https://github.com/tailscale/tailcat/releases>
- <https://tailscale.com/tailcat>

Tailcat is BSD-3-Clause licensed. No Tailcat source or binary is committed to this
repository. The on-demand runtime installation preserves the official `LICENSE` and
`README.md`; the PowerShell launcher only constructs documented CLI invocations.
