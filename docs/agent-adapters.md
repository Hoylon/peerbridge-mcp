# Agent Integration Boundaries

PeerBridge accepts any safe `--agent-id`; that does not automatically turn every model
subscription or web application into an MCP coding agent.

## Capability union, not a lowest-common-denominator profile

PeerBridge evaluates capabilities per Agent, per adapter, and per room Seat. A missing
feature in one vendor adapter must never hide or disable that feature for another adapter.
For example, if one installed transport cannot prove interactive approval support, only
that adapter is marked `conditional` or `unsupported`; every verified adapter keeps its
own approval controls.

The public adapter contract uses three states:

- `supported`: implemented by this adapter and covered by an adapter-specific test;
- `conditional`: available only when the installed runtime advertises or proves it;
- `unsupported`: unavailable on this adapter only. PeerBridge fails closed for that
  operation without downgrading other Agents in the room.

The room capability view is therefore a union. Execution remains exact-route: PeerBridge
does not silently replace an official route with a relay, another vendor, or a weaker
transport.

## Interactive runtime approval

Runtime tool approval is separate from PeerBridge's peer-review quorum. The composer
offers three modes for a managed Agent session:

| Mode | Behavior |
| --- | --- |
| `approval-required` | Display each provider request and wait for allow once, allow for this session, or deny. |
| `agent-delegated` | Allow routine operations supported by that adapter; escalate risky requests to the operator. |
| `full-access` | Ask once when starting the governed session, then use the provider's full-access profile until that session stops. |

Current adapter mapping:

| Agent | Preferred transport | Interactive approval |
| --- | --- | --- |
| Codex | native app-server JSON-RPC | Supported through command, file-change, and permission approval requests. |
| Claude Code | native stream-json control | Supported through `can_use_tool` stdio control requests and responses. |
| Grok | native ACP | Supported through `session/request_permission`. |
| Kimi Code | native ACP | Supported through `session/request_permission`. |

If a selected adapter cannot perform the requested mode, the UI must identify that exact
Agent and route and refuse the operation. It must not claim approval occurred, remove the
same control from capable Agents, or reroute through a relay without an explicit operator
choice.

## MCP-native client

Use this route when the coding client can launch a stdio MCP server or connect to an
authenticated Streamable HTTP MCP endpoint. The client remains responsible for its model
session and calls PeerBridge tools directly.

Examples include Codex, Claude Code, and Kimi Code CLI. Each should receive a distinct
agent ID and share the same project root, scope, and database.

PeerBridge prefers this path whenever a real client or terminal can consume MCP. The
control room labels these sessions `MCP NATIVE`. The client still has to be running,
authorized, and configured; a saved route cannot wake a closed third-party client.

## Official website, official service, and relay route are different

The same model family can be available through more than one route:

- an official website session, such as `grok.com` or the DeepSeek website;
- an official API or official MCP-capable client;
- a relay or gateway that offers a provider-labelled model channel.

These routes are not interchangeable. An open, signed-in web tab is not automatically an
MCP client. A relay entry also does not prove that it is byte-for-byte identical to the
official service. PeerBridge records operator-supplied route labels; it does not attest to
the upstream model's provenance.

If an existing coding client can select Grok or DeepSeek through a relay, no model download
is required. Launch a separate client session, assign a unique `--agent-id`, and record the
safe runtime labels:

```text
--agent-id grok-relay-reviewer
--client-name relay-coding-client
--provider-id relay:grok-official-channel
--model-id grok
```

An independently adapted official website or official API session must use a different
agent and provider label, for example `grok-official-reviewer` and `xai-official-web`.
Never put a provider URL containing credentials, API key, cookie, or account name in these
labels.

## API-backed runner

Use this route when a provider exposes model and tool-calling APIs but no suitable coding
client. A separate runner owns the model loop, translates provider tool calls to an
allowlisted read-only subset of PeerBridge MCP, enforces cumulative operation budgets,
and records its provider/model identity. The control room labels this `MCP TOOL`; it is
not represented as a native model-side MCP session.

DeepSeek and Grok may be connected this way if the chosen official or relay route cannot
consume MCP directly. API access, a web subscription, a relay channel, and an installed
desktop app are different capabilities and must not be treated as interchangeable.

The CC Switch Claude fallback is deliberately narrower: it runs one bounded inference
with tools disabled and is labelled `INFERENCE`. It can answer a dispatched prompt, but
it cannot claim that Claude called PeerBridge tools. Use Claude Code's direct MCP
configuration when tool access is required.

## Test ladder

1. Run synthetic identities to test leases, mailbox ordering, quorum, and disconnection.
2. Run one real MCP client against a synthetic project and verify the full audit trail.
3. Add a second real client and intentionally create path conflicts and stale proofs.
4. Test each provider adapter independently; never infer adapter correctness from core tests.
5. Only then test a mixed-provider workload with explicit budgets and a human kill switch.

No real provider is required to validate steps 1 and 2. Do not buy or install additional
providers until the intended client or runner interface is known.
