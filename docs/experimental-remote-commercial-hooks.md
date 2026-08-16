# Experimental Remote and Commercial Hooks

## Release boundary

此版定位為 **local Alpha / remote Experimental**。Remote/mobile 是獨立、預設 **OFF**
的模組，必須由 operator 明確啟用；Experimental 不代表 managed service、production SLA
或一般公開網路服務已可用。

本機 open-source core 維持免費且可完整使用。Local coordination、audit、Control Room、
provider-neutral interfaces 與 self-hosted remote building blocks 不得因 entitlement、
waitlist 或未來商業服務而失效。

目前 remote 路徑可使用使用者自行管理的 Tailscale 環境，但 PeerBridge 不宣稱
Tailscale 本身需要付費。Tailscale 的供應、方案與條件由其自身決定；未來 PeerBridge
收費若成立，價值來自整合服務，而不是把 Tailscale 描述成付費障礙。

## Future paid value

未來可能的 paid remote offering 可販售以下營運價值，但本版沒有宣稱它們已上線：

| Offering | Commercial value |
| --- | --- |
| Integrated onboarding | Device、identity、network setup、diagnostics 與 guided activation |
| Reliability | Health monitoring、reconnect、upgrade coordination 與 incident handling |
| Managed sync and recovery | Managed state sync、backup、restore 與 recovery workflow |
| Managed push | Cross-device notification delivery 與 delivery diagnostics |
| Support | Onboarding assistance、priority troubleshooting 與明確的 support plan |

收費邊界只能涵蓋這些整合與代管能力，不能回頭鎖住已發布的本機核心或必要的本機安全、
audit 與資料匯出能力。

## Dormant commercial hooks

下列 hooks 可以隨此版以 interface、schema 或 disabled configuration 形式存在，但必須保持
dormant。Hook 存在不等於服務已啟用、可購買或已部署。

本機實作可用 `peerbridge product --project-root . status` 查詢 capability manifest。
Manifest 明示 `commercial_services_active=false`、`entitlement_provider=none`；它不會
建立帳號、連線到商業服務或把 unavailable capability 偽裝成已啟用。

### Capability and entitlement interface

- 使用 stable capability IDs 查詢 `available`、`experimental`、`requires_activation` 或
  `unavailable`，不得用模糊的 account side effect 判斷。
- `core.local` 與既有 self-hosted 能力永遠不需 commercial entitlement；entitlement 只可
  控制未來 managed capabilities，例如 `managed.sync`、`managed.push` 與 `managed.support`。
- 沒有 provider 或網路時必須 fail closed for managed features，同時保持 local core 正常。
- 查詢不得自行建立帳號、傳送 installation data 或收集 credentials。

### Stable and beta update channels

- Channel 只能由 operator 選擇，預設 `stable`；切換 channel 不構成 telemetry consent。
- 此 Alpha release 可以只攜帶 channel metadata；hook 不代表 auto-update 已實作。
- Beta 必須明確標示風險並可回到 stable，不得暗中把既有安裝移入 beta。

### Signed manifests

- Release manifest 應固定包含 schema、channel、version、artifact URL、byte size、SHA-256、
  required core version、capability IDs 與 signature metadata。
- Client 必須先驗 signature，再驗 artifact hash；未知 key、錯誤 signature、channel mismatch
  或 downgrade 都必須停止安裝。
- Manifest 只描述 release 與 capability，不可夾帶 telemetry endpoint、user targeting、
  credentials 或任意 executable configuration。

### Opt-in waitlist and activation

- Waitlist、remote module enable、commercial activation 與 telemetry 是四個分開的 consent；
  任一同意不得代替其他同意。
- UI/CLI 必須在送出前顯示 endpoint 與完整 payload，並允許取消或撤回。
- Dormant hook 不得 background enroll、預先建立 account，或從 project content 猜測聯絡資料。
- 本版沒有 activation service；沒有明示成功 receipt 就只能顯示 unavailable，不能假裝已啟用。

### Provider plugin boundary

- Managed sync、push、entitlement 與 update provider 應透過 versioned plugin contract 與
  local core 分離；plugin install 不等於 plugin activation。
- Plugin 只取得被授權 capability 的最小資料，預設不能讀 prompts、message bodies、
  file names/paths、model outputs、private project names 或 provider secrets。
- Provider failure、移除或不相容不得損壞 local data，也不得阻止 local core 啟動。
- Plugin 必須公開 provider identity、network endpoints、requested capabilities 與 data schema。

## Explicitly out of scope

此版不得加入或啟用 payment/checkout code、billing SDK、subscription enforcement、hidden
telemetry、credential collection、background account enrollment、managed sync/push backend，
或任何 cloud deployment。也不得為了測試 hook 上傳 API keys、tokens、Tailscale credentials、
project content 或 model/provider credentials。

## Measurable launch funnel

| Stage | Measurable signal | Interpretation |
| --- | --- | --- |
| Discovery | GitHub traffic views/clones | GitHub traffic 只有 recent window，未保存的舊資料不可回補。 |
| Download | Release asset `download_count` | 是 downloads，不是 unique users、installations 或 sessions。 |
| Local activation | Distinct opt-in `installation_activated` IDs | 只有 opt-in app events 才能計數。 |
| Active use | Opt-in daily/7d/30d active installation IDs and session totals | Active user counts require opt-in app events；installation/session 都不等於 person。 |
| Remote interest | Explicit waitlist submissions | 與 telemetry consent 分離，且目前沒有 waitlist endpoint。 |
| Experimental enable | Explicit local remote enable receipts; optional allowlisted event | 預設 OFF；未 opt-in telemetry 的 enable 不會進入 analytics。 |
| Managed activation | Signed activation receipts | 本版沒有 activation service，因此此數值目前不可量測，不能填估計值。 |
| Paid conversion | Completed commercial transaction | 本版沒有 payment code，conversion 應標示 unavailable，而不是 `0` 或推估。 |

`download_count` 可作為 release distribution 趨勢，不能當分母宣稱精確 user conversion。
完整 telemetry event、daily aggregation、export/reset/disable 與 retention 約束以
`docs/telemetry.md` 為準。
