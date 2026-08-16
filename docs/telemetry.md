# Telemetry and Launch Metrics

## Current status

PeerBridge 已實作一個 dependency-free 的本機 daily aggregate store 與 CLI，但預設
**OFF**。目前沒有 analytics sender、collector 或預設 endpoint，因此即使 operator
選擇加入，資料仍只保留在作業系統帳戶的 PeerBridge application-state 目錄。GitHub
自行提供的流量與 release
asset 計數不屬於 PeerBridge 應用程式遙測。

任何日後的網路 sender／collector 都必須先符合本文件、公開 event schema 與透明
endpoint，並再次由使用者明確選擇加入。安裝、升級、選擇 update channel 或啟用
Experimental remote 均不得自動開啟遙測。

## Local controls

```powershell
peerbridge analytics --project-root . status
peerbridge analytics --project-root . enable
peerbridge analytics --project-root . record --event feature_used --dimension feature=local_core
peerbridge analytics --project-root . export
peerbridge analytics --project-root . reset
peerbridge analytics --project-root . disable
```

`status` 與未 opt-in 的 `record` 不建立 analytics state。`export` 只輸出公開 schema 的
daily aggregates；`reset` 輪替隨機 installation ID 並清除舊 aggregate；`disable` 清除
本機 aggregate 並阻止後續收集。這些命令都不執行網路請求。

目前 Alpha **沒有自動期限清理**：使用者明確 opt-in 後，本機 daily aggregates 會一直
保留，直到使用者執行 `reset`／`disable` 或自行刪除 application-state 目錄。下文的 7 日
期限是未來 sender/collector 的上限，不是現版本已實作的行為。

預設 state 位置是 Windows `%LOCALAPPDATA%\PeerBridge\analytics`、macOS
`~/Library/Application Support/PeerBridge/analytics`，以及 Linux
`${XDG_STATE_HOME:-~/.local/state}/peerbridge/analytics`。測試或 portable automation 可用
`PEERBRIDGE_ANALYTICS_HOME` 指定另一個本機目錄；這只改變儲存位置，不構成 opt-in。

## Consent and overrides

- 預設為 **OFF**；沒有肯定的 opt-in 就不建立 installation ID、不排隊、不傳送。
- 同意必須在 endpoint、event allowlist、保存期限顯示後取得，且可隨時撤回。
- `DNT=1` 或 `DO_NOT_TRACK=1` 一律強制停用。PeerBridge CLI 不會讀取瀏覽器設定；若
  browser wrapper 要遵從 browser DNT，必須把它明確轉成上述環境變數。
- `PEERBRIDGE_TELEMETRY=0` 強制停用；`PEERBRIDGE_TELEMETRY=1` 可作為明確的
  administrator opt-in。停用優先於任何既有同意。
- `PEERBRIDGE_TELEMETRY_ENDPOINT` 只選擇透明或 self-hosted endpoint，本身不構成
  opt-in，也不得觸發資料傳送。目前 Alpha 沒有 sender，因此不讀取這個保留變數；
  日後加入 sender 時才可依公開 schema 啟用。

## Identity and daily aggregation

Opt-in 後才為目前作業系統帳戶產生一個隨機、opaque、可重設的 128-bit installation
ID；不同 project roots 共用同一 ID，避免把同一安裝灌水成多個使用者。它不得由帳號、
硬體、IP、hostname、檔案系統、project 或 tailnet identity 推導。重設會立即換成無法
連結的新 ID，並清除未送出的舊 ID 資料。

客戶端只建立 UTC date 粒度的 daily aggregates，不送出精確操作時間或逐筆使用軌跡。
session 只累加每日次數，不建立跨日或可追蹤的 session ID。

允許的共同欄位只有：`schema_version`、`utc_date`、`installation_id`、`app_version`、
`os_family`、`arch`、`update_channel` 與 `count`。事件專屬欄位必須使用下表的固定 enum；
client 與 endpoint 都必須拒絕未知 event、未知欄位、free text 與任意 metadata。

| Event | Purpose | Additional allowlisted fields |
| --- | --- | --- |
| `installation_activated` | Opt-in 後首次啟用；每個 ID 一次 | None |
| `installation_active` | 每個 UTC date 最多一次的 active installation | None |
| `session_started` | 每日 session 次數 | None |
| `feature_used` | 每日粗粒度功能使用量 | `feature`: `local_core`, `control_room`, `experimental_remote` |
| `operation_outcome` | 每日可靠度總計 | `operation`: fixed public enum; `outcome`: `success`, `failure`, `cancelled` |
| `update_result` | Update channel 健康度 | `result`: `none`, `available`, `installed`, `signature_failed`, `download_failed` |

## Data that is never allowed

任何 event、log、error 或附加欄位都不得包含：

- prompts、message bodies 或其摘要；
- API keys、tokens、passwords、cookies 或其他 secrets；
- file names、file paths、command arguments 或 environment values；
- model outputs、tool outputs、stack traces 或任意 free text；
- private project names、scope names、room names、provider account identity；
- raw IP、hostname、user name、email、tailnet login 或硬體識別碼。

不得以 hash、編碼、截斷或「匿名化」名義收集上述內容。來源 IP 或 user agent 不得寫入
analytics store，也不得用來合併 installation identities。

## User controls and endpoint transparency

本機實作提供可離線使用的 `status`、`export`、`reset` 與 `disable` 控制：

- `status` 顯示目前 consent、endpoint、schema、installation ID 建立日期與本機 aggregate
  數量。因 Alpha 沒有 sender，所以沒有 queue 或最近傳送日期。
- `export` 匯出本機保留及已送出的同型 JSON daily aggregates，不加入額外診斷資料。
- `reset` 輪替 installation ID 並清除本機 aggregates。日後有 endpoint 時，遠端刪除必須
  另有可驗證 receipt，不能假裝目前已完成。
- `disable` 立即停止本機收集並清除 aggregates，且不影響本機核心功能。

啟用畫面必須顯示完整 HTTPS endpoint、schema version、保存政策與 self-hosted 選項。
官方 endpoint 若日後存在，不得隱藏在 redirect 後；自架 collector 必須使用相同公開
schema 與 export/delete contract。endpoint 不可用時只能保留受期限約束的 queue，
不得 fallback 到另一個未顯示的服務。

## Retention policy

未來 sender／collector 的上限如下：本機未送 daily aggregates 保存 7 日；server 上仍含隨機
installation ID 的 daily rows 保存 30 日；移除 installation ID 後的總體 counters 保存
13 個月。`reset` 或 `disable` 的刪除要求應在 30 日內自 primary storage 清除舊 ID rows；
任何備份不得再延長超過 30 日。Self-hosted operator 可以縮短期限，但必須在 opt-in 前
顯示實際政策。

## Launch funnel and exact limits

| Funnel stage | Metric | Source and limitation |
| --- | --- | --- |
| Release interest | GitHub views/clones | GitHub traffic 只提供 recent window；未定期保存就不能重建長期趨勢。 |
| Asset acquisition | Sum of GitHub release asset `download_count` | `download_count` 是下載次數，不是 unique users；重試、自動化與同一人多次下載都可能增加計數。 |
| Opt-in activation | Distinct `installation_activated` IDs | 只涵蓋明確 opt-in installations，不代表所有下載或使用者。 |
| Active installations | Distinct `installation_active` IDs per day/7d/30d | Active user counts 必須依賴 opt-in app events；installation 不是 person。 |
| Sessions | Sum of `session_started` daily counts | Session 是啟動／使用區間，不是 installation，也不是 unique user。 |
| Experimental remote use | Distinct opt-in IDs with `feature_used=experimental_remote` | 模組預設關閉；未 opt-in telemetry 的使用不會被計數。 |

不得把 GitHub downloads、active installations 與 sessions 混稱為「users」。下載到啟用的
比率最多是 aggregate directional signal，不是 cohort conversion，也不能補推未 opt-in 的
active user 數量。
