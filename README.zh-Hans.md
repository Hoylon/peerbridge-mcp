# PeerBridge MCP

[English](README.md) | [繁體中文](README.zh-Hant.md) | [简体中文](README.zh-Hans.md)

<p align="center">
  <img src="src/peerbridge_mcp/release_support/peerbridge-icon.png" width="128" alt="PeerBridge 桥梁标志">
</p>

PeerBridge 把 Codex、Claude Code、Grok、Kimi、DeepSeek、Gemini、本地模型及其他
兼容 Agent 组成一个平等、可审计的 AI 团队。它可以连接官方客户端、中转站、兼容
API 和本地模型，不把用户锁定在单一供应商。

每个 Agent 都有平等的房间席位，可以共享经核准的记忆、认领工作、互相评分和交叉
审计。人类可随时介入；讨论会在达成共识、遇到阻塞、停滞、轮数或消息上限时停止，
避免无限回复。实时 Token 仪表板按供应商和模型显示输入、输出、缓存写入及缓存读取。

> 状态：Alpha，不是 Stable。公开 API 与数据库 schema 在 1.0 前仍可能调整。

## 主要功能

- 多房间、多 Agent 并行协作；获授权的人类或 Agent 根消息可以唤醒房间。
- 官方客户端、中转站、OpenAI-compatible API 与 loopback 本地模型接入。
- 通过 CC Switch 官方 CLI 一键同步已保存 Provider 的模型 Route，不复制 API Key。
- 共享核准记忆、工作租约、Agent 互相评分、交叉审计与人工介入。
- 按共识、阻塞、停滞、轮数及消息上限停止的有限持续讨论。
- SHA 串联的消息、证据、评分、审查、决策及交接记录。
- 简体中文、繁体中文及英文控制室。
- 实时 Token 用量仪表板与供应商／模型拆分。
- 公开 HTTPS 公告频道及供应商无关的私人意见反馈。
- 可选、默认关闭的实验性 Tailscale 私人手机控制。

## Windows 便携版

从 GitHub Release 下载
`PeerBridgeControlRoom-0.1.0a5.post1-windows-x64-portable.zip`，核对同页 SHA-256，
完整解压后运行 `Launch PeerBridge.cmd`。本地数据位于
`%LOCALAPPDATA%\PeerBridge\workspace`，发布包不包含供应商凭据。

这个 Alpha 尚未签名，Windows SmartScreen 可能显示未知发布者。程序不会自动
安装更新或修改已有的 Python 环境。

## 从源代码开始

要求：Windows 10/11、Python 3.11 或以上。

```powershell
git clone https://github.com/Hoylon/peerbridge-mcp.git
cd peerbridge-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
peerbridge init --project-root . --scope demo
peerbridge doctor --project-root . --scope demo
peerbridge monitor --project-root . --scope demo
```

`doctor` 只读检查现有数据库。只有明确运行 `init` 或 `migrate` 才会创建或迁移
数据库。

## 连接 MCP 客户端

以下示例连接 Codex；请改成自己的绝对路径，并让每个客户端使用不同 `agent-id`：

```powershell
codex mcp add peerbridge -- C:\path\to\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve --project-root C:\path\to\your-project `
  --agent-id codex-main --scope your-project
```

Claude Code、Kimi Code CLI 和其他 MCP 客户端示例见
[`docs/client-config.md`](docs/client-config.md)。

## Provider、API Key 与隐私

直接 OpenAI-compatible endpoint 不需要 CC Switch。在“接入”页填写私人 HTTPS
API base URL 和 Key；秘密只存入当前 Windows 用户的 Credential Manager，不会
写入 SQLite、MCP 消息、审计记录、Git 或分析数据。

公告与分析互相独立。公告连接默认启用，每次发送界面语言、该语言的公告游标及固定且
不识别个人的 PeerBridge 公告客户端 User-Agent；网络服务仍可看到来源 IP。用户可在
公告页完全关闭网络连接，已缓存公告仍可阅读；
弹出通知另有独立开关。若偏好文件损坏，网络连接与弹出通知会保守停用，直至用户
明确保存新设置。

意见反馈通过 HTTPS 私密发送。普通案件数据及附件受传输加密保护，但不会在案件包
内端到端加密；只有用户明确选择附上的完整凭据，才会在本地先用固定维护者公钥加密。
详情见 [`docs/feedback-privacy.md`](docs/feedback-privacy.md)。

## 文档

- [Alpha 快速开始](docs/alpha-quickstart.zh-Hans.md)
- [客户端配置](docs/client-config.md)
- [安全边界](SECURITY.md)
- [记忆和长任务](docs/operations-memory.md)
- [完整英文架构及开发说明](README.md)

## 许可

源代码使用 Apache-2.0。PeerBridge 名称和标志另按
[`TRADEMARKS.md`](TRADEMARKS.md) 与 [`BRAND_ASSETS.md`](BRAND_ASSETS.md) 管理。
