# Experimental Tailcat remote toolkit

PeerBridge keeps **Tailscale Serve** as its production browser path. Tailcat is an
optional companion for direct encrypted CLI connectivity when an operator needs a
temporary port, protected SSH proxy, file transfer, SOCKS5 command, or exit-node
experiment without installing a Tailscale control-plane client on both machines.

Tailcat does not replace PeerBridge authorization. Its connection token identifies a
WireGuard endpoint; it is not a room identity, review approval, or MCP capability. The
PeerBridge remote page still requires its independent launcher credential and, in
production, an allowed Tailscale identity.

## Supported modes

`scripts/launch_tailcat_remote.ps1` exposes the official Tailcat CLI features as
separate foreground modes:

| Mode | Purpose | Additional gate |
| --- | --- | --- |
| `PortServer` / `PortClient` | Forward one localhost TCP port | Allowed client nodekey |
| `ReceiveFiles` | Write-only drop box | Allowed client nodekey |
| `ServeFiles` | Read-only file service by default | `-EnableWrite` for read/write |
| `SshServer` / `SshClient` | Proxy to the machine's existing authenticated SSH port | Allowed client nodekey |
| `CopyToServer` / `CopyFromServer` | Tailcat/SFTP copy | Token read from a file |
| `SocksCommand` | Run one command through Tailcat SOCKS5 | Explicit command path |
| `ExitNode` | Reach the server-side network | `-EnableExitNode` plus allowed client nodekey |

The launcher deliberately rejects Tailcat's `no-auth-ssh` mode. It uses
`--key=new` for every server run, verifies the caller-supplied SHA-256 of
`tailcat.exe`, rejects reparse-point executables and served roots, and never downloads or
bundles a binary. It runs in the foreground so closing it destroys the ephemeral server
key and listener.

## One-time client identity

Create a client key with the official CLI on the client machine:

```powershell
tailcat genkey --client --key=peerbridge-client
```

Pass the printed public `nodekey:...` to the server launcher. It is a public key, not the
private client key. Example for a protected local port:

```powershell
.\scripts\launch_tailcat_remote.ps1 `
  -Mode PortServer `
  -TailcatPath C:\Tools\tailcat.exe `
  -ExpectedSha256 <official-release-sha256> `
  -Port 8765 `
  -AllowClientKey nodekey:<client-public-key>
```

The official Tailcat process prints an ephemeral `tc...` connection token. Store it in a
current-user protected file on the client, then use `PortClient`, `SshClient`, or another
client mode with `-TokenFile`. Do not put tokens in Git, screenshots, issue reports, or
PeerBridge evidence.

## Browser and phone boundary

Tailcat's current browser WebAssembly demo transfers text and files, but browser traffic is
DERP-relayed and it is not a general authenticated HTTP tunnel for the PeerBridge control
room. A normal phone browser also cannot run the desktop Tailcat CLI. Therefore:

- phone and normal browser access continue through Tailscale Serve;
- desktop CLI and controlled command traffic may use this Tailcat toolkit;
- Tailcat browser/mobile control remains experimental until the upstream WebRTC/direct
  path and a separately authenticated PeerBridge browser gateway are proven.

Do not expose the unauthenticated local WebView workbench through Tailcat. Do not use an
experimental Tailcat run as the strict remote/mobile release receipt.

Official references:

- <https://github.com/tailscale/tailcat>
- <https://github.com/tailscale/tailcat/releases>
- <https://tailscale.com/tailcat>
- <https://github.com/tailscale/tailcat/issues/4>
