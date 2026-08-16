# Client Configuration

Install PeerBridge into a dedicated virtual environment first. Use its absolute Python
path in client configuration so GUI applications do not depend on shell activation.

## Codex

Codex supports local stdio MCP servers in the desktop app, CLI, and IDE extension. These
clients share the same Codex MCP configuration on one host.

### CLI

Windows PowerShell:

```powershell
codex mcp add peerbridge -- C:\tools\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve `
  --project-root C:\work\my-project `
  --agent-id codex-main `
  --scope my-project
codex mcp list
```

Linux or macOS:

```bash
codex mcp add peerbridge -- /opt/peerbridge-mcp/.venv/bin/python \
  -m peerbridge_mcp serve \
  --project-root /work/my-project \
  --agent-id codex-main \
  --scope my-project
codex mcp list
```

### `config.toml`

```toml
[mcp_servers.peerbridge]
command = "C:\\tools\\peerbridge-mcp\\.venv\\Scripts\\python.exe"
args = [
  "-m", "peerbridge_mcp", "serve",
  "--project-root", "C:\\work\\my-project",
  "--agent-id", "codex-main",
  "--scope", "my-project",
]
enabled = true
required = true
default_tools_approval_mode = "prompt"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

Codex reads user configuration from `~/.codex/config.toml` and trusted project
configuration from `.codex/config.toml`.

## Claude Code

### CLI

```powershell
claude mcp add --scope project --transport stdio peerbridge -- `
  C:\tools\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve `
  --project-root C:\work\my-project `
  --agent-id claude-code `
  --scope my-project
claude mcp list
claude mcp get peerbridge
```

Project-scoped servers require workspace trust and explicit approval in Claude Code.

### `.mcp.json`

```json
{
  "mcpServers": {
    "peerbridge": {
      "type": "stdio",
      "command": "C:\\tools\\peerbridge-mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "-m", "peerbridge_mcp", "serve",
        "--project-root", "${CLAUDE_PROJECT_DIR:-.}",
        "--agent-id", "claude-code",
        "--scope", "my-project"
      ]
    }
  }
}
```

## Kimi Code CLI

Kimi Code CLI can register the same stdio server. Give it a distinct agent ID:

```powershell
kimi mcp add --transport stdio peerbridge -- `
  C:\tools\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve `
  --project-root C:\work\my-project `
  --agent-id kimi-code `
  --scope my-project
kimi mcp list
```

An equivalent ad-hoc configuration is available in
[`examples/kimi-mcp.json`](../examples/kimi-mcp.json). Authenticate Kimi through its own
supported login flow; never place its token in PeerBridge arguments or messages.

## Any other MCP-capable client or terminal

PeerBridge is not limited to the three clients above. Any client that can launch a local
stdio MCP server can use the same command shape:

```text
C:\tools\peerbridge-mcp\.venv\Scripts\python.exe -m peerbridge_mcp serve \
  --project-root C:\work\my-project \
  --agent-id <one-stable-unique-agent-id> \
  --scope <one-shared-project-scope> \
  --client-name <actual-client-name> \
  --provider-id <non-secret-provider-identity> \
  --model-id <observed-model-identity>
```

Register that command through the client's documented MCP configuration surface. A web
chat tab or a plain REST model endpoint is not an MCP client. API-only models can instead
use PeerBridge's bounded allowlisted tool-loop and are labelled `MCP TOOL`; a tools-disabled
one-shot fallback is labelled `INFERENCE`.

Grok and DeepSeek can each coexist as an official route and as a relay-provided route. If
the relay's coding client can call MCP, launch one separately identified session per route.
An official web tab alone is not an MCP client. See
[agent integration boundaries](agent-adapters.md) before adding either route.

## Shared configuration rules

- All entries must use the same `--project-root`, database, and `--scope`.
- Each client must use a different stable `--agent-id`.
- Record non-secret `--client-name`, `--provider-id`, and `--model-id` labels whenever a
  client can switch provider/model routes.
- Do not point unrelated projects at the same database.
- Do not store provider API keys in MCP arguments, messages, or the SQLite store.
- A connected server does not wake the other model. Each client must be running or be
  invoked by its own supported automation mechanism.

## Verification

1. Run `peerbridge doctor --project-root <root> --scope <scope>`.
2. In Codex, use `/mcp` or `codex mcp list`.
3. In Claude Code, use `/mcp` or `claude mcp list`.
4. In Kimi Code CLI, use `/mcp` or `kimi mcp list`.
5. Ask each client to call `bridge_status`; all should report the same database and scope.
6. Ask each client to call `workboard`; their agent IDs should appear while online.

Sources:

- [Official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp)
- [Official Kimi Code CLI repository](https://github.com/MoonshotAI/kimi-cli)
