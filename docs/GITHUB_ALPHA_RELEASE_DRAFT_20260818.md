# PeerBridge MCP v0.1.0-alpha.5.1

This maintenance release restores the visible chat attachment picker, keeps every Windows
entry point on the native PeerBridge-branded executable, focuses an existing control room
on repeated launch, and fixes Alpha 5.1 update discovery.

## 繁體中文

這個維護版本修復聊天附件視窗不可見、確保所有 Windows 入口均使用帶有 PeerBridge
Logo 的原生執行檔、重複啟動時聚焦既有控制室，並修正 Alpha 5.1 更新辨識。

PeerBridge 將 Codex、Claude Code、Grok、Kimi、DeepSeek、Gemini、本機模型及其他
相容 Agent 組成一個平等、可稽核的 AI 團隊。它支援官方客戶端、中轉站、相容 API
與本機模型，讓使用者不被單一供應商綁定。

### 核心功能

- 多房間、多 Agent 平行協作，任何獲授權的人類或 Agent 根訊息均可喚醒房間。
- 共享核准記憶、工作認領、Agent 互相評分與交叉審計。
- 即時 Token 儀表板，按供應商與模型查看輸入、輸出、快取寫入及快取讀取。
- 透過 CC Switch 官方 CLI 一鍵同步已保存 Provider 的模型 Route，不複製 API Key。
- 討論按共識、阻塞、停滯、輪數或訊息上限停止，避免無限回覆。
- SHA 串接的訊息、證據、評分、審查、決策及交接記錄。
- 繁體中文、簡體中文及英文介面。
- 公開 HTTPS 公告頻道，以及透過 HTTPS 私密送出的供應商無關意見回饋；只有使用者
  明確選擇附上的憑證會在本機先以維護者公鑰端對端加密。

### 安裝

下載 `PeerBridgeControlRoom-0.1.0a5.post1-windows-x64-portable.zip`，核對同頁公布的
SHA-256，完整解壓後執行 `Launch PeerBridge.cmd`。本機資料位於
`%LOCALAPPDATA%\PeerBridge\workspace`，發布包不包含供應商憑證。

本版本是未簽署的公開 Alpha 版本（非 Stable）。Windows SmartScreen 可能提示未知發布者。

## English

PeerBridge brings Codex, Claude Code, Grok, Kimi, DeepSeek, Gemini, local models, and
other compatible Agents into one equal, auditable AI team. It supports official clients,
relay services, compatible APIs, and local models without locking the user to one
provider.

### Highlights

- Parallel multi-room Agent collaboration where an authorized human or Agent root post
  can wake the room.
- Approved shared memory, task ownership, mutual Agent scoring, and cross-agent audits.
- Live Token dashboards by provider and model for input, output, cache writes, and cache
  reads.
- One-click CC Switch model-route synchronization through its official CLI without
  copying API keys.
- Bounded discussion that stops on consensus, blockers, stagnation, round limits, or
  message limits.
- SHA-linked messages, evidence, scores, reviews, decisions, and handoffs.
- Traditional Chinese, Simplified Chinese, and English desktop interfaces.
- A public HTTPS announcement channel and provider-independent private feedback over
  HTTPS; only credentials explicitly included by the user are encrypted end-to-end to
  the maintainer support public key before leaving the device.

### Install

Download `PeerBridgeControlRoom-0.1.0a5.post1-windows-x64-portable.zip`, verify the published
SHA-256, extract the archive completely, and run `Launch PeerBridge.cmd`. Local state is
stored under `%LOCALAPPDATA%\PeerBridge\workspace`; provider credentials are never
included in the release archive.

This is an unsigned public Alpha release. Windows SmartScreen may show an unknown-publisher
warning.
