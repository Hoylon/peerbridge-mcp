# Tailcat remote toolkit

PeerBridge keeps **Tailscale Serve** as its production browser path. Tailcat is an
enabled-by-default companion for direct encrypted CLI connectivity when an operator needs
the PeerBridge port, protected SSH forwarding, file transfer, SOCKS5, or exit-node access
without installing a Tailscale control-plane client on both machines.

Tailcat does not replace PeerBridge authorization. Its connection token identifies a
WireGuard endpoint; it is not a room identity, review approval, or MCP capability. The
PeerBridge remote page still requires its independent launcher credential and, in
production, an allowed Tailscale identity.

## Supported modes

The local Workbench uses `TailcatRuntimeManager` to install the pinned official Windows
release and run `ManagedServer`. `scripts/launch_tailcat_remote.ps1` also exposes the
official Tailcat CLI features as separate foreground modes:

| Mode | Purpose | Additional gate |
| --- | --- | --- |
| `PortServer` / `PortClient` | Forward one localhost TCP port | Allowed client nodekey |
| `ReceiveFiles` | Write-only drop box | Allowed client nodekey |
| `ServeFiles` | Read-only file service by default | `-EnableWrite` for read/write |
| `SshServer` / `SshClient` | Proxy to the machine's existing authenticated SSH port | Allowed client nodekey |
| `CopyToServer` / `CopyFromServer` | Tailcat/SFTP copy | Token read from a file |
| `SocksCommand` | Run one command through Tailcat SOCKS5 | Explicit command path |
| `ExitNode` | Reach the server-side network | `-EnableExitNode` plus allowed client nodekey |
| `ManagedServer` | Start PeerBridge port, SSH, and exit node together | Persistent server key, allowed client nodekey, owned Job Object |

The launcher deliberately rejects Tailcat's `no-auth-ssh` mode. Standalone server modes use
`--key=new`; managed mode uses a protected persistent key so its address survives normal
restarts. Both paths verify `tailcat.exe`, reject reparse-point executables and served
roots, and run in the foreground. PeerBridge owns managed mode in a Windows kill-on-close
Job Object, so the master switch or application shutdown stops the full process tree.

## Default-on managed mode

The preference defaults to enabled. On the first local desktop launch PeerBridge:

1. downloads only `tailcat_0.4.0_windows_amd64.zip` from the official GitHub release;
2. verifies archive SHA-256
   `c238a4e8d3b460423a67e5ad400888b73ffa0b28e15173fd32c9acb699a3a89e`;
3. safely extracts only `tailcat.exe`, `LICENSE`, and `README.md`, then verifies executable
   SHA-256 `bcb0c6c91e126ee9a5880e45fe067484a1bc056d721447d5fae8575ab6e672bc`;
4. creates a protected server identity and owner client identity under
   `.peerbridge/tailcat`;
5. starts one allow-listed service for the selected PeerBridge port, port 22, and
   `exit-node`.

Installation and provisioning run in a background thread so the control room remains
responsive. The UI reports each real phase and never labels the service ready merely
because its launcher script exists. Turning the master switch off persists the preference
and stops the owned process. Turning it on or reopening PeerBridge starts it again.

## Pairing a controlled device

Managed mode generates `pairing/owner-client.private.json` plus `pairing/CONNECT.txt`.
Use the local Workbench's **Open pairing folder** command and copy that folder only to a
device you control. The private JSON file is the client identity and must not be posted,
logged, or committed. The connection address by itself is insufficient because the server
also enforces the generated client public-key allow-list.

Standalone operators may instead create a client key on the client machine:

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

Standalone Tailcat prints an ephemeral `tc...` connection token. Store it in a current-user
protected file on the client, then use `PortClient`, `SshClient`, or another client mode
with `-TokenFile`. Do not put tokens or private client keys in Git, screenshots, issue
reports, or PeerBridge evidence.

## Browser and phone boundary

Tailcat's current browser WebAssembly demo transfers text and files, but browser traffic is
DERP-relayed and it is not a general authenticated HTTP tunnel for the PeerBridge control
room. A normal phone browser also cannot run the desktop Tailcat CLI. Therefore:

- phone and normal browser access continue through Tailscale Serve;
- desktop CLI and controlled command traffic may use this Tailcat toolkit;
- Tailcat browser/mobile control remains experimental until the upstream WebRTC/direct
  path and a separately authenticated PeerBridge browser gateway are proven.

Do not expose the unauthenticated local WebView workbench through Tailcat. The forwarded PeerBridge
port must be the separately authenticated remote backend. Tailcat runtime evidence does not
replace the strict Tailscale remote/mobile release receipt.

Official references:

- <https://github.com/tailscale/tailcat>
- <https://github.com/tailscale/tailcat/releases>
- <https://tailscale.com/tailcat>
- <https://github.com/tailscale/tailcat/issues/4>
