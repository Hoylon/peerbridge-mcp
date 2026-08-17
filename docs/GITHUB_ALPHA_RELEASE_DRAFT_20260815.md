# GitHub Pre-release Draft: PeerBridge MCP v0.1.0-alpha.3

Use this text only after the frozen artifacts pass every gate and the operator authorizes
publication. Mark the GitHub release as
**Pre-release**. Do not mark it Latest or Stable.

## PeerBridge MCP v0.1.0-alpha.3

## 繁體中文

PeerBridge 是一個本機優先、可稽核的協作層，讓多個平等的 AI coding Agent
與人類操作員在同一個控制室內協作。這個 Alpha 提供共享 SQLite mailbox、
有範圍限制的 writer lease、房間式 Agent 協作、有限度的平行討論、審查／證明記錄、
具來源鏈的持久記憶，以及像素風本機控制室。

### 包含功能

- 本機 stdio MCP 協作；核心執行只需要 Python 3.11+。
- 可持久保存的房間與可重用 Agent 席位。
- 人類或 Agent 的根訊息可喚醒房間內已配置的 Agent；討論有輪數、訊息、停滯及共識上限，避免無限回覆。
- 每個席位可選供應商、模型與推理強度，模型清單可動態探索。
- 官方 Codex 模型目錄探索，包括當前可用的 Sol、Terra、Luna 變體，不把執行時模型清單寫死。
- 繁體中文、簡體中文與英文桌面介面。
- 首次啟動教學、唯讀更新檢查、安全的本機圖片／文字附件，以及供應商無關的私密 HTTPS 意見回饋。
- 完整憑證診斷只在使用者明確同意後，於本機以維護者公鑰加密；正常診斷不包含秘密內容。
- 預設關閉、只留在本機的彙總使用量分析；此 Alpha 沒有遙測傳送器。
- 有界記憶體／資源控制與 crash recovery gate。

### 安裝發布資產

**Windows 可攜版：**下載 `PeerBridgeControlRoom-0.1.0a3-windows-x64-portable.zip`，
核對發布頁的 SHA-256，完整解壓後執行 `Launch PeerBridge.cmd`。這是未簽署 Alpha，
Windows SmartScreen 可能會提示警告。本機資料存放在
`%LOCALAPPDATA%\PeerBridge\workspace`，發布包不包含任何供應商憑證。

**Python wheel：**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .\peerbridge_mcp-0.1.0a3-py3-none-any.whl
peerbridge init --project-root . --scope demo
peerbridge doctor --project-root . --scope demo
peerbridge-monitor --project-root . --scope demo
```

### 已驗證候選版

- Python 自動測試：**617 collected，616 passed，1 intentionally skipped，0 failed**。
- HTTPS 收件端：**43/43 Cloudflare Worker tests** 與 **15/15 private mail receiver tests** 通過。
- 記憶體 soak：1,200 個訊息；最後八個樣本的 private-memory plateau growth 為 409,600 bytes，低於 24 MiB 上限；zero-write receipt verification 通過。
- 發布前須以最終 frozen manifest 再執行一次 zero-write verification。
- Wheel、source distribution 與 Windows 可攜版的位元組數及 SHA-256 以最終 GitHub Release 所列值為準。

### Alpha 限制

- 1.0 前 public API 與 SQLite schema 仍可能改變。
- 尚未提供簽署的 Windows installer、簽署的一鍵更新器與自動回滾。
- Windows 可攜版未簽署，以解壓即用 ZIP 發布。
- 實驗性 self-hosted remote/mobile 程式碼預設關閉，不屬於本機 Alpha 的支援或證據範圍。
- 不包含 managed cloud sync、付費遠端服務、原生 iPhone App 或手機 UI 重製。
- 真實多供應商能力取決於使用者自己的帳戶、憑證、配額及模型可用性；發布包不提供任何供應商憑證。

### 私隱與支援

供應商憑證只保留在作業系統 credential store，不會進入專案資料庫、MCP 訊息、
一般意見回饋、使用量分析或 Git 歷史。此 Alpha 的分析預設關閉，且沒有傳送器。
請勿把私人資料庫、逐字稿、API key 或供應商設定貼到公開 GitHub issue；私密回報方式請參閱 `SECURITY.md`。

安裝或回報問題前，請先閱讀：

- [Alpha 支援矩陣](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-support-matrix.md)
- [Alpha 疑難排解](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-troubleshooting.md)
- [繁體中文快速開始](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-quickstart.zh-Hant.md)
- [簡體中文快速開始](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-quickstart.zh-Hans.md)

---

## English

PeerBridge is a local-first, auditable coordination layer for equal AI coding peers and their
human operator. This Alpha provides a shared SQLite mailbox, scoped writer leases, room-based
Agent collaboration, bounded parallel discussion, review/proof records, durable memory with
provenance, and a pixel-style local control room.

### What is included

- Local stdio MCP coordination with no required runtime dependencies beyond Python 3.11+.
- Persistent rooms and reusable Agent seats.
- Human or Agent root-post fanout with bounded discussion and no reply cascades.
- Per-seat provider, model, and reasoning selection with dynamic model catalogs.
- Official Codex catalog discovery, including the currently available Sol, Terra, and Luna
  variants, without hard-coded runtime model lists.
- Traditional Chinese, Simplified Chinese, and English desktop UI.
- First-run tutorial, read-only update check, safe local image/text attachments, and private
  provider-independent HTTPS feedback with optional end-to-end public-key encryption for a
  complete credential diagnostic.
- Default-off, local-only aggregate analytics. This Alpha has no analytics sender.
- Bounded memory/resource controls and crash-recovery gates.

### Install from the release asset

**Windows portable app:** download
`PeerBridgeControlRoom-0.1.0a3-windows-x64-portable.zip`, verify its published SHA-256,
extract the complete archive, and run `Launch PeerBridge.cmd`. The unsigned Alpha may
trigger Windows SmartScreen. It stores local state under
`%LOCALAPPDATA%\PeerBridge\workspace` and includes no provider credentials.

**Python wheel:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .\peerbridge_mcp-0.1.0a3-py3-none-any.whl
peerbridge init --project-root . --scope demo
peerbridge doctor --project-root . --scope demo
peerbridge-monitor --project-root . --scope demo
```

### Verified candidate

- Automated tests: **617 collected, 616 passed, 1 intentionally skipped, 0 failed**.
- Edge intake tests: **43 Cloudflare Worker tests and 15 private mail-receiver tests passed**.
- Memory soak: 1,200 messages with 409,600 bytes of private-memory plateau growth in the final
  eight samples, below the 24 MiB acceptance limit; zero-write receipt verification passed.
- Continuity manifest: verify the final frozen manifest with zero writes before publication.
- Wheel and source-distribution byte counts and SHA-256 values: copy them from the local
  create-only `.peerbridge/receipts/local-alpha-release-final-v3.json` generated after the
  tracked release source is frozen. Do not copy hashes from an earlier candidate directory.

### Alpha limitations

- The public API and SQLite schema may change before 1.0.
- A signed native Windows installer and signed one-click updater are not included.
- The portable Windows executable is unsigned and is distributed as an extract-and-run ZIP.
- Experimental self-hosted remote/mobile code ships in the source distribution but is
  default-off, unsupported, and outside the local Alpha evidence/support boundary.
- Managed cloud sync, paid remote service, native iPhone, and mobile UI redesign are not included.
- Real multi-provider behavior depends on the user's own provider accounts, credentials, limits,
  and model availability. This release does not include provider credentials.
- Physical disposable send/remove/route acceptance was completed on Windows. Any later change
  to drag geometry, routing persistence, or attachment transport requires that acceptance to
  be repeated before publishing.

### Privacy and support

Provider credentials remain in the operating-system credential store and do not enter the
project database, MCP messages, normal feedback, analytics, or Git history. Analytics is off by
default and has no sender in this Alpha. Do not attach private databases, transcripts, API keys,
or provider configuration to public GitHub issues. Follow `SECURITY.md` for private reporting.

Before installing or reporting a problem, review:

- [Alpha support matrix](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-support-matrix.md)
- [Alpha troubleshooting](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-troubleshooting.md)
- [繁體中文快速開始](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-quickstart.zh-Hant.md)
- [简体中文快速開始](https://github.com/oscarho200407-hue/peerbridge-mcp/blob/v0.1.0-alpha.3/docs/alpha-quickstart.zh-Hans.md)

Source-code license: Apache-2.0. The PeerBridge name and logo are governed separately
by `TRADEMARKS.md` and `BRAND_ASSETS.md`; those notices are included in every artifact.
