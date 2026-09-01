# Interface and remote research sources

This release uses three user-supplied product references. The mapping below separates
source code reuse from behavioral inspiration.

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

The default-off launcher wraps the official Tailcat CLI without bundling or modifying its
binary. It exposes separate modes for ports, authenticated SSH proxying, file transfer,
SOCKS5 commands, and an explicitly gated exit-node experiment. PeerBridge requires the
operator to supply and verify the official binary SHA-256.

- <https://github.com/tailscale/tailcat>
- <https://github.com/tailscale/tailcat/releases>
- <https://tailscale.com/tailcat>

Tailcat is BSD-3-Clause licensed. No Tailcat source is copied into this repository; the
PowerShell launcher only constructs documented CLI invocations.
