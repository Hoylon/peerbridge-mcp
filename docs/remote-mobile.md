# Private remote and mobile control

PeerBridge can expose its complete responsive human control room to devices in the same Tailscale
tailnet without renting a server. This is **not** a public MCP endpoint and it is not a
multi-tenant cloud service.

## Data path

```mermaid
flowchart LR
    P["Phone or remote computer"] -->|"Tailnet HTTPS"| TS["Tailscale Serve"]
    TS -->|"Identity header + loopback proxy"| WEB["PeerBridge remote UI\n127.0.0.1 only"]
    WEB -->|"read-only snapshot"| DB[("Project SQLite")]
    WEB -->|"human message via stdio MCP"| MCP["PeerBridge MCP process"]
    MCP --> DB
```

The Python backend refuses every non-loopback bind. Tailscale Serve terminates HTTPS,
authenticates the tailnet user, and injects `Tailscale-User-Login`. PeerBridge compares
that identity against an allowlist. Every API request must also carry a separate 256-bit
launcher credential. The backend receives it through a child-process environment variable,
stores only its SHA-256, and never accepts it as a command-line argument. It persists only a
SHA-derived human agent ID and never writes the raw login to SQLite or the audit chain.

The responsive desktop/mobile page can:

- switch among visible rooms and inspect scope-bound messages, tasks, registered routes,
  providers, Agent presence, and dispatch/tool activity;
- send an explicit human message through the same MCP `send_message` tool as the desktop
  control room;
- create a bounded room, cancel a queued/running governed operation, verify the audit
  chain, and keep browser language/tutorial preferences;
- pause, resume, continue, or stop an already active room discussion through the existing
  audited `control_discussion` MCP tool.

The same navigation and status components remain visible remotely, but local-authority
controls are disabled in the browser and rejected again by a server-side allowlist. Remote
access cannot run a shell, launch or install an Agent, upload an attachment, import history,
edit provider credentials, switch CC Switch, enqueue new execution workflows, change room
membership, apply a patch, approve a review, or modify project files.

The interface follows the same information model on a wide desktop and a phone. Desktop uses
a persistent room rail, work timeline, and inspector. Phone uses a room drawer, compact
Conversation / Work switch, fixed composer, and safe-area-aware controls. No voice or upload
button is shown because the current remote backend deliberately denies microphone, camera,
and arbitrary file permissions.

## Default-on Tailcat companion

Tailcat is visible and enabled by default, not hidden behind an advanced disclosure. The
local desktop installs the pinned official Windows release after verifying its archive and
executable SHA-256, creates protected project-local identities, and starts one owned server
for the PeerBridge port, authenticated SSH forwarding, and exit-node access. A small master
switch stops or restores that process. File transfer and client-side SOCKS5 remain on-demand.

Tailcat does not replace the authenticated browser path. Its browser WASM demo is not a
production PeerBridge HTTP tunnel and remains DERP-relayed. See the
[Tailcat remote toolkit](tailcat-remote.md) for the exact boundary and launcher.

## Start on Windows

Requirements:

1. PeerBridge is installed into the repository's `.venv`.
2. Tailscale is signed in on the always-on PC.
3. The phone or remote computer is signed into the same tailnet.
4. MagicDNS and HTTPS certificates are enabled for the tailnet. Tailscale requires a
   tailnet administrator to acknowledge that the machine and tailnet DNS names become
   public certificate-transparency metadata.

The launcher fails closed before starting PeerBridge when `tailscale status --json`
reports no certificate domain. This avoids an indefinite first-run consent prompt and
leaves no loopback backend running. Enable HTTPS once in the Tailscale admin DNS page,
then rerun the launcher.

Run:

```powershell
.\scripts\launch_remote_control.cmd -Port 8765 -Scope your-project
```

The launcher starts a hidden loopback process, waits for `/healthz`, and configures
Tailscale Serve. Its internal recovery file retains the launcher credential under a
current-user Windows ACL, but users should not share that file or copy its URL manually.
Use **Copy private link** or **Share to phone** in the local Remote page. Each click creates
a new 15-minute, one-use pairing URL. Messaging applications may carry its query code
without receiving the real workbench bearer. A link-preview GET cannot consume it; the
browser performs a same-origin POST, the backend verifies the Tailscale login, exchanges
the code for a session-only token, and removes the code from browser history before loading
room data. Do not put either link in logs, issues, screenshots, or release evidence.

Do not use Tailscale Funnel; Funnel is public internet exposure and is outside this design.

The local Remote page provides Copy and Share-to-phone commands only after the launcher,
Serve mapping, Funnel-off state, ownership record, and credential hash all bind to the same
healthy instance. Share uses the operating system's Web Share sheet when available and
falls back to copying the private link; PeerBridge never sends the link to a cloud broker.
Its guided setup has separate Phone and Another computer modes. Both use the same three
checks: join the same tailnet, start/verify the local remote workspace once, then open the
private link. The second computer needs Tailscale and a browser but no PeerBridge install.
Tailcat remains visible in this page. Its status says installing, provisioning, starting,
running, stopped, or failed rather than treating a launcher script as a ready service. The
generated pairing folder contains the private client identity and bounded connection
commands; copy it only to a device you control.

The `.ts.net` origin normally stays stable for the same machine in the same tailnet, but it
is not a universal PeerBridge address and different machines/tailnets receive different
origins. The private fragment credential belongs to one launcher instance and may rotate
after restart, recovery, or reconfiguration. Multiple approved people can reach the same
origin only when each person's Tailscale login is explicitly allowed; the private link must
still be handled as a secret. Managed Tailcat uses a protected persistent server identity so
its address survives normal PeerBridge restarts. Standalone launcher modes still use
`--key=new` unless the caller selects `ManagedServer` with an explicit key file.

By default, only the owner identity of the local Tailscale node is authorized. Advanced
operators can launch `peerbridge remote` directly with repeated `--allow-login` values.
The values remain process configuration; PeerBridge does not persist them.

## Security controls

- loopback-only backend and tailnet-only HTTPS proxy;
- exact Tailscale login allowlist;
- independent 256-bit private-link credential on every API call, with only its SHA-256
  retained by the backend;
- access URL stored in a current-user-only Windows ACL file and never placed in process
  arguments, SQLite, Serve state, or logs;
- per-process CSRF token and strict HTTPS same-origin checks;
- JSON and field allowlists, 64 KiB request limit, 20,000-character message limit;
- 12 human writes per identity per minute;
- ten-second socket timeout and connection close after each response;
- no-store, CSP, frame denial, referrer denial, and browser permission denial headers;
- scope filtering in SQLite before row limits are applied;
- credential-pattern rejection before the MCP write path;
- raw provider endpoints, credential targets, and credential fingerprints excluded from
  the remote snapshot;
- all writes use the existing MCP schema validation and SHA-linked audit chain.

Forging the Tailscale identity header alone is insufficient: the independent private-link
credential is also required. A process running as the same local operating-system account
can still read that account's protected URL file or process memory, so the local account
remains a trusted boundary. Other non-administrator operating-system accounts do not
receive access through loopback-header forgery. Do not run untrusted software under the
PeerBridge account.

Official Tailscale references:

- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)

## Verification

Before relying on remote access, run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_remote.py
.\.venv\Scripts\python.exe -m peerbridge_mcp doctor `
  --project-root . --db .peerbridge\live.sqlite3 --scope your-project
```

The automated tests cover identity denial, forged-header denial without the private-link
credential, protected URL-file ACLs, absence of the credential from process arguments and
logs, scope isolation, CSRF, same-origin HTTPS, credential rejection, MCP-path writes,
audit verification, same-port restart without message loss, launcher ownership, and
validated Serve configuration. They do not replace a real device test.

## Release evidence gate

Strict release is deliberately fail-closed until a real phone has connected, disconnected,
and reconnected through the private tailnet URL. A desktop browser, emulator, fixture,
mock, copied Boolean assertion, or launcher unit test does not satisfy this gate.

Run the device test against the exact source tree that will be packaged:

1. Start PeerBridge with the Windows launcher and record the private `https://...ts.net`
   origin. Do not enable Funnel.
2. On a physical phone signed into the same tailnet, open the control room, authenticate,
   load its snapshot, and send a harmless uniquely identified human message.
3. Close the browser session or disconnect the phone from Tailscale. Record the disconnect
   time, reconnect, open a fresh browser session, and load the snapshot again.
4. Verify the SHA-linked audit chain after the reconnect and bind the phone message ID.
5. Capture Serve state/status, Funnel status, local listener ownership, the phone browser
   trace, and audit verification as UTF-8 JSON files under
   `.peerbridge/evidence/<run-id>/`. Capture commands, exit codes, exact byte counts, and
   SHA-256 values are part of the receipt.
6. Write a create-only receipt at
   `.peerbridge/receipts/remote-mobile-e2e-v2.json`, then run strict release.

The receipt schema is `peerbridge.remote-mobile-e2e-receipt.v2`. It binds:

- the current release source-tree SHA-256;
- the exact scope, private `.ts.net` origin, and `http://127.0.0.1:<port>` backend;
- `tailnet_only=true`, `funnel_enabled=false`, `test_mode=false`, and
  `evidence_origin=real-device`;
- exactly six artifacts named `serve_state`, `serve_status`, `funnel_status`,
  `network_observation`, `browser_trace`, and `audit_verification`;
- each artifact's repository-relative path, bytes, SHA-256, capture command, and zero exit
  code;
- two authenticated phone sessions (`initial`, then `reconnect`) with different hashed
  browser-session nonces, the same hashed tailnet login and browser-local-storage device
  nonce, per-session user-agent and snapshot hashes, mobile viewport/touch observations,
  the same PeerBridge instance identity, and an intervening operator-marked disconnect;
- a hashed disconnect challenge, a bounded minimum/observed reconnect interval, and explicit
  `false` values for network-layer node identity and cryptographic disconnect proof. These
  browser observations must not be described as proof that the Tailscale node went offline;
- a successful remote MCP message that is visible again in the reconnect snapshot, plus the
  later valid audit head that contains it.

Raw logins, user agents, peer addresses, API keys, access tokens, passwords, cookies, and
provider credentials must not be placed in evidence. Store only the required SHA-256
identities. Evidence paths must stay below `.peerbridge/evidence/`; the receipt itself is
excluded from release artifacts.

Strict release command:

```powershell
.\.venv\Scripts\python.exe .\scripts\release_check.py --release
```

The gate rehashes every evidence file, verifies the receipt's own canonical SHA-256,
rehashes the current source tree, rejects test/simulation markers, rejects any Funnel or
public listener, and verifies the reconnect chronology and audit linkage. Missing or stale
evidence stops before wheel/sdist construction and prints
`RELEASE_CHECK_FAILED release_ready=false`.
