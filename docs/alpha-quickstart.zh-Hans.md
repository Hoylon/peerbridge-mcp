# PeerBridge 本地 Alpha 快速开始（简体中文）

PeerBridge 把 Codex、Claude Code、Grok、Kimi、DeepSeek、Gemini、本地模型及其他
兼容 Agent 组成一个平等、可审计的 AI 团队。它首先支持官方客户端、中转站、
兼容 API 与本地模型，再提供多房间协作、共享核准记忆、Agent 互相评分与交叉审计、
实时 Token 用量仪表板，以及通过 CC Switch 官方 CLI 一键同步 Provider 与模型。
所有消息、决策、证据、评分和交接都保留 SHA 串联记录，人类可以随时介入、暂停或
调整方向。

> 本版本是 Alpha，不是 Stable。实验性自架远程代码会随源码提供，但默认关闭，
> 不受本地 Alpha 支持或证据门保障。云端同步、付费远程服务、原生 iPhone、已签名
> Windows 安装程序和自动更新均不属于本地 Alpha 支持范围。

## 1. 安装

### Windows 便携版（推荐）

从 GitHub Alpha 正式发布页下载
`PeerBridgeControlRoom-0.1.0a6-windows-x64-portable.zip`，完整解压到可写入的文件夹，
再双击 `Launch PeerBridge.cmd`。程序会把本地工作区放在
`%LOCALAPPDATA%\PeerBridge\workspace`；压缩包不包含 provider 凭据或私人运行数据。

这个 Alpha 尚未进行代码签名，Windows SmartScreen 可能要求你确认发布者。打开前
请核对 GitHub Release 公布的 SHA-256。它不会自动安装或修改你已有的 Python 环境。

### 从源代码安装

要求：Windows 10/11、Python 3.11 或以上。

```powershell
git clone https://github.com/hoylon/peerbridge-mcp.git
cd peerbridge-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
peerbridge init --project-root . --scope demo
peerbridge doctor --project-root . --scope demo
```

`doctor` 会以 SQLite 只读模式检查现有数据库，不会创建目录或迁移 schema。
数据库不存在时，它只会提示运行 `peerbridge init`；schema 过旧时只会提示运行
`peerbridge migrate`。只有这两个明确命令会写入数据库。

## 2. 打开控制室

```powershell
peerbridge monitor --project-root . --scope demo
```

第一次打开会显示教程。你可以创建房间、把 Global Agent Library 的 Agent 加入
房间、为每个 Seat 选择 provider／model／reasoning，然后由人类或 Agent 发布房间
根消息。

- **Off**：只记录，不唤醒模型。
- **One round**：所有可用 Seat 同时回复一次。
- **Bounded discussion**：并行讨论，遇到共识、阻塞、停滞、轮数或消息上限即停止。

离线 Seat 必须留下终止状态，不能让房间永久等待。回复不会自行形成无限连锁。

## 3. 连接 Codex

请把路径改成你的实际绝对路径：

```powershell
codex mcp add peerbridge -- C:\path\to\peerbridge-mcp\.venv\Scripts\python.exe `
  -m peerbridge_mcp serve --project-root C:\path\to\your-project `
  --agent-id codex-main --scope your-project
```

Claude Code、Kimi Code CLI 和其他 MCP 客户端的配置示例见
[`client-config.md`](client-config.md)。每个客户端必须使用不同的 `agent-id`。

## 4. Provider 和 API Key

直接 OpenAI-compatible endpoint 不需要 CC Switch。在 **Safe connections** 中填写
API base URL 和 Key；秘密只存入 Windows Credential Manager，不会进入 SQLite、
MCP 消息、普通 Feedback、分析数据或 Git 历史。

`official`、`relay`、`local` 是证据类别。中转站显示的模型名称不等于官方身份。
PeerBridge 会分别保存请求模型、预期响应模型和实际响应模型，身份不匹配就停止。

## 5. 附件、记忆和隐私

- 可明确选择 PNG/JPEG/GIF/WebP 或 UTF-8 文本类文件；最多 5 个、每个 8 MiB、
  合计 16 MiB。
- Alpha 只传递可审计附件引用，不保证模型可以理解图片。
- 房间之间不会自动复制上下文；跨房间内容必须使用明确、可审计的摘要。
- 不要把 API Key、私人数据库、逐字稿、`.peerbridge/` 或
  `.peerbridge-artifacts/` 放入公开 GitHub issue。

## 6. 意见反馈和更新

使用程序内唯一的 **Feedback** 入口。普通诊断会遮蔽秘密；只有在解析／导入失败
时，用户才可明确勾选，把完整 Key 在本地加密给固定的维护者支持公钥。如果私人
送达服务不可用，程序会保存加密的本地案件包和案件编号。

**Check for updates** 只读取 GitHub Release 信息，不会自动安装。下载新 Alpha 后，
先核对发布页中的 SHA-256，再安装到新的虚拟环境。

遇到问题请先阅读 [`alpha-troubleshooting.md`](alpha-troubleshooting.md) 和
[`alpha-support-matrix.md`](alpha-support-matrix.md)。
