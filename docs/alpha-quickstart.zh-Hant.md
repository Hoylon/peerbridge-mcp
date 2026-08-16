# PeerBridge 本機 Alpha 快速開始（繁體中文）

PeerBridge 是本機優先、可稽核的多 Agent 協作層。這個 Alpha 使用共用 SQLite
信箱、工作租約、房間、互評、證據及 SHA 串接事件，讓 Codex、Claude Code 或其他
已配置的 Agent 在不互相覆寫工作的情況下協作。

> 本版本是 Alpha，不是 Stable。實驗性自架遠端程式碼會隨原始碼提供，但預設關閉、
> 不受本機 Alpha 支援或證據閘門保障。雲端同步、付費遠端服務、原生 iPhone、已簽署
> Windows 安裝程式及自動更新均不包括在本機 Alpha 支援範圍內。

## 1. 安裝

### Windows 可攜版（建議）

從 GitHub Alpha pre-release 下載
`PeerBridgeControlRoom-0.1.0a2-windows-x64-portable.zip`，完整解壓到可寫入的資料夾，
再雙擊 `Launch PeerBridge.cmd`。程式會把本機工作區放在
`%LOCALAPPDATA%\PeerBridge\workspace`；壓縮檔不包含 provider 憑證或私人執行資料。

這個 Alpha 尚未進行程式碼簽署，Windows SmartScreen 可能要求你確認發布者。開啟前
請核對 GitHub Release 公布的 SHA-256。它不會自動安裝或修改你原有的 Python 環境。

### 從原始碼安裝

需求：Windows 10/11、Python 3.11 或以上。

```powershell
git clone https://github.com/oscarho200407-hue/peerbridge-mcp.git
cd peerbridge-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
peerbridge init --project-root . --scope demo
peerbridge doctor --project-root . --scope demo
```

`doctor` 會以 SQLite 唯讀模式檢查既有資料庫，不會建立目錄或遷移 schema。
資料庫不存在時，它只會提示執行 `peerbridge init`；schema 過舊時只會提示執行
`peerbridge migrate`。只有這兩個明確命令會寫入資料庫。

## 2. 開啟控制室

```powershell
peerbridge monitor --project-root . --scope demo
```

第一次開啟會顯示教學。你可以建立房間、把 Global Agent Library 的 Agent 加入
房間、為每個 Seat 選擇 provider／model／reasoning，然後由人類或 Agent 發出房間
根訊息。

- **Off**：只記錄，不喚醒模型。
- **One round**：所有可用 Seat 同時回覆一次。
- **Bounded discussion**：平行討論，遇到共識、阻塞、停滯、輪數或訊息上限便停止。

離線 Seat 必須留下終止狀態，不可令房間永久等待。回覆不會自行形成無限連鎖。

## 3. 連接 Codex

請把路徑改成你的實際絕對路徑：

```powershell
codex mcp add peerbridge -- C:\path\to\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve --project-root C:\path\to\your-project `
  --agent-id codex-main --scope your-project
```

Claude Code、Kimi Code CLI 及其他 MCP 客戶端的設定例子見
[`client-config.md`](client-config.md)。每個客戶端必須使用不同 `agent-id`。

## 4. Provider 和 API Key

直接 OpenAI-compatible endpoint 不需要 CC Switch。在 **Safe connections** 內填寫
API base URL 和 Key；秘密只存入 Windows Credential Manager，不會進入 SQLite、
MCP 訊息、一般 Feedback、分析資料或 Git 歷史。

`official`、`relay`、`local` 是證據類別。中轉站顯示的模型名稱不等於官方身份。
PeerBridge 會分開保存請求模型、預期回應模型及實際回應模型，身份不符便停止。

## 5. 附件、記憶及私隱

- 可明確選擇 PNG/JPEG/GIF/WebP 或 UTF-8 文字類檔案；最多 5 個、每個 8 MiB、
  合計 16 MiB。
- Alpha 只傳遞可稽核附件引用，不保證模型能理解圖片。
- 房間之間不會自動複製上下文；跨房間內容必須使用明確、可稽核的摘要。
- 不要把 API Key、私人資料庫、逐字稿、`.peerbridge/` 或
  `.peerbridge-artifacts/` 放到公開 GitHub issue。

## 6. 意見回饋及更新

使用程式內唯一的 **Feedback** 入口。一般診斷會遮蔽秘密；只有在解析／匯入失敗
時，使用者才可明確勾選，把完整 Key 在本機加密給固定的維護者支援公鑰。若私人
送達服務不可用，程式會保存加密本機案件包及案件編號。

**Check for updates** 只讀取 GitHub Release 資訊，不會自動安裝。下載新 Alpha 後，
先核對發佈頁面的 SHA-256，再安裝到新的虛擬環境。

遇到問題請先閱讀 [`alpha-troubleshooting.md`](alpha-troubleshooting.md) 和
[`alpha-support-matrix.md`](alpha-support-matrix.md)。
