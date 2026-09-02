# PeerBridge MCP Alpha 6

Alpha 6 turns PeerBridge into one coherent local, remote-desktop, and mobile control room
for auditable multi-Agent engineering.

## Highlights

- A redesigned conversation-first Workbench with compact navigation, adjustable desktop
  side panels, integrated code review, safe rich Agent output, and transient per-turn file
  changes backed by a permanent Work ledger.
- The complete room, task, Agent, activity, review, audit, memory, and usage workspace is
  available through private Tailscale Serve on desktop and phone.
- Mobile navigation now scrolls independently and closes by its X button, outside backdrop,
  or direct Chat destination. The collaboration summary and composer default to one compact
  row so conversation content remains primary.
- Copy/Share now creates a 15-minute, one-use pairing link that survives messaging-app URL
  fragment stripping. The real session credential is issued only after Tailscale identity
  and same-origin verification, then removed from browser history.
- Tailcat v0.4.0 integration is visible and enabled by default. PeerBridge verifies the
  official archive and executable hashes, provisions protected identities, and owns one
  client-key-allow-listed Port, SSH, and Exit-node process with a local master switch.
- Windows taskbar identity now uses the packaged PeerBridge icon consistently, and remote
  revalidation works from both PowerShell 7 and Windows PowerShell 5.1 launch paths.

## Security boundaries

- The backend remains loopback-only; Tailscale Serve is the production browser transport
  and Funnel must remain disabled.
- Tailcat never enables `no-auth-ssh`, never starts with an empty client allow-list, and is
  terminated with its owned Windows Job Object.
- Provider credentials, private pairing material, local context imports, and project
  databases remain outside release assets and rendered evidence.
- This remains an Alpha release. Provider availability and experimental transports are not
  claims of stable third-party service support.

## Install

1. Download `PeerBridgeControlRoom-0.1.0a6-windows-x64-portable.zip`.
2. Verify it against `SHA256SUMS.txt` on this release.
3. Extract the complete archive to a writable folder.
4. Run `Launch PeerBridge.cmd`.

## 繁體中文摘要

Alpha 6 把本機桌面、遠端電腦及手機整合成同一個可稽核多 Agent 工作台。手機側欄可完整
滑動並可按 X、右側空白遮罩或「對話」返回；「目前協作」與輸入框預設只佔一行。私人
分享連結改為 15 分鐘一次性配對碼，並在 Tailscale 身份驗證後才換取 session credential。
Tailcat 預設顯示及啟用，但仍使用 client-key allow-list、SHA 驗證與受管程序生命週期。
