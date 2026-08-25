# PeerBridge MCP v0.1.0-alpha.5.3

## English

Alpha 5.3 completes the provider-native approval and adapter layer for the local
PeerBridge workspace.

- Codex, Claude Code, Grok, and Kimi Code keep separate official adapter identities.
- Capabilities are shown per Agent; one vendor limitation no longer disables another
  Agent's verified feature.
- Codex JSON-RPC, Claude `can_use_tool`, and Grok/Kimi ACP permission requests appear as
  auditable Allow once, Allow for session, or Deny cards.
- Modern and Pixel workspaces include real appearance previews, clearer three-language
  onboarding, model and reasoning controls, Agent activity, diffs, evidence, and token
  usage.
- Provider, relay, local, and CC Switch routes stay distinct. PeerBridge never silently
  changes an official route to a relay.

Security hardening in this release removes title-based grant reuse, restricts Claude
session grants to visible same-tool rules, and caps approval fan-out. The final
post-remediation security scan found no surviving issues.

## 繁體中文

Alpha 5.3 完成 PeerBridge 本機工作區的多廠商適配器與原生權限核准層。

- Codex、Claude Code、Grok、Kimi Code 保持各自獨立的官方適配器身分。
- 能力按每個 Agent 顯示；某一廠商缺少功能，不會再關閉其他 Agent 已驗證的能力。
- Codex JSON-RPC、Claude `can_use_tool`、Grok/Kimi ACP 權限要求會顯示為可審計的
  「批准一次／本工作階段允許／拒絕」卡片。
- Modern 與 Pixel 介面加入真實預覽、三語首次教學、模型與推理強度、Agent 活動、
  程式碼差異、證據及 Token 使用量。
- 官方、中轉、本機與 CC Switch 路由保持分離，不會靜默把官方路由切換至中轉。

本版亦移除標題式權限重用、限制 Claude 工作階段授權範圍，並限制核准請求併發量；
修復後的最終安全掃描沒有存續問題。

## 简体中文

Alpha 5.3 完成 PeerBridge 本地工作区的多厂商适配器与原生权限批准层。

- Codex、Claude Code、Grok、Kimi Code 保持各自独立的官方适配器身份。
- 能力按每个 Agent 显示；某个厂商缺少功能，不会关闭其他 Agent 已验证的能力。
- Codex JSON-RPC、Claude `can_use_tool`、Grok/Kimi ACP 权限请求显示为可审计的
  “批准一次／本工作阶段允许／拒绝”卡片。
- Modern 与 Pixel 界面加入真实预览、三语首次教程、模型与推理强度、Agent 活动、
  代码差异、证据及 Token 使用量。
- 官方、中转、本地与 CC Switch 路由保持分离，不会静默把官方路由切换到中转。

本版同时移除标题式权限复用、限制 Claude 工作阶段授权范围，并限制批准请求并发量；
修复后的最终安全扫描没有遗留问题。

