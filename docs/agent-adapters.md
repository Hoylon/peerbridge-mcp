# Agent Integration Boundaries

PeerBridge accepts any safe `--agent-id`; that does not automatically turn every model
subscription or web application into an MCP coding agent.

## MCP-native client

Use this route when the coding client can launch a stdio MCP server or connect to an
authenticated Streamable HTTP MCP endpoint. The client remains responsible for its model
session and calls PeerBridge tools directly.

Examples include Codex, Claude Code, and Kimi Code CLI. Each should receive a distinct
agent ID and share the same project root, scope, and database.

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
client. A separate runner owns the model loop, translates provider tool calls to MCP calls,
enforces workspace and token limits, and records its provider/model identity. This runner
is future work; it is not part of v0.1.

DeepSeek and Grok may be connected this way if the chosen official or relay route cannot
consume MCP directly. API access, a web subscription, a relay channel, and an installed
desktop app are different capabilities and must not be treated as interchangeable.

## Test ladder

1. Run synthetic identities to test leases, mailbox ordering, quorum, and disconnection.
2. Run one real MCP client against a synthetic project and verify the full audit trail.
3. Add a second real client and intentionally create path conflicts and stale proofs.
4. Test each provider adapter independently; never infer adapter correctness from core tests.
5. Only then test a mixed-provider workload with explicit budgets and a human kill switch.

No real provider is required to validate steps 1 and 2. Do not buy or install additional
providers until the intended client or runner interface is known.
