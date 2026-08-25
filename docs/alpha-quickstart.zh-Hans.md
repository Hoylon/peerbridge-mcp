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
`PeerBridgeControlRoom-0.1.0a5.post4-windows-x64-portable.zip`，完整解压到可写入的文件夹，
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

全新工作区第一次启动会先显示两张本地预览：**Pixel Control Room** 和
**Modern Workbench**。选择会保存在本地，重启后应用；Modern 左下的“外观”
按钮或 Pixel 顶部的外观菜单都可以重新选择。它不会下载或加载第三方主题代码。

在 **01 多智能体控制台** 查看当前房间智能体及已授权的桌面或外部终端工作，无需先
选择文件夹。启动新的受管 CLI 时可选择已安装的 Codex、Claude Code、Grok 或 Kimi 和
工作文件夹；终端墙会并列显示每个工作阶段的已清理输出、后续输入、附件和生命周期控制。
工作内容通过受控输入通道发送。所有角色都在 **02 对话** 的房间座位设置，
默认是平等参与者。你可以用网格、聚焦或时间线查看各自的终端、活动、答案和证据。
PeerBridge 只显示实际捕捉或经 adapter 明确授权的输出，不会声称看到隐藏思考，也
不能补回在其他外部终端中发生的历史。

控制台顶部的实时状态栏会直接显示每个 Agent 的模型／路线、权限层级和在线状态；
红色表示离线、绿色表示在线待命、琥珀色表示正在执行、蓝色表示等待。活动文字只来自
可观察的本地事件，例如读取、编辑、执行命令或搜索网络，不会显示或猜测私密思维链。
在 **变更** 页面可查看当前 Git 工作树逐文件的新增／删除行数及经过秘密遮蔽的代码 diff。

要完成首次多智能体流程，先在 **02 对话** 准备至少两个已有模型路线的房间座位，再点击
“启动只读调查与比较”。它会沿用同一房间、角色和路线，最多执行两轮；不会重开另一组
智能体。对应的持久作业会显示在 **09 信任与任务验证工作**，可取消、超时、重试，并在
程序重启后恢复跟踪。只有讨论真正完成时才会留下完成收据。

在 **09 信任与任务验证工作** 可选择 Implement + Review、Investigate + Debate、Read-only Audit
或 Release Gate 模板，并管理本地作业队列、计划、权限决策、隔离 Git worktree、
Trust Timeline、任务简报、冲突和 Proof Bundle。Cloud collaboration 在 Alpha 5.2
保持停用；本地 SQLite 仍是唯一协作权威。

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

直接 OpenAI-compatible endpoint 不需要 CC Switch。在 **接入** 中填写 API base URL
和 Key、实时读取供应商模型并创建 Agent 路由；秘密只存入 Windows Credential Manager，不会进入 SQLite、
MCP 消息、普通 Feedback、分析数据或 Git 历史。

同一页面可通过 CC Switch 公共 CLI 读取已保存的供应商和模型。切换当前供应商必须另行
确认；PeerBridge 不会在官方配额耗尽时自动切换到中转站。

`official`、`relay`、`local` 是证据类别。中转站显示的模型名称不等于官方身份。
PeerBridge 会分别保存请求模型、预期响应模型和实际响应模型，身份不匹配就停止。

## 5. 附件、记忆和隐私

- 可明确选择或直接粘贴 PNG/JPEG/GIF/WebP 图片，也可选择 UTF-8 文本类文件；最多 5 个、每个 8 MiB、
  合计 16 MiB。
- 支持原生图片输入的 runtime 会收到多模态内容；一次性视觉验证只有在模型读出隐藏
  测试内容后才标记成功。不支持或未登录的 runtime 会明确失败。
- 房间之间不会自动复制上下文；跨房间内容必须使用明确、可审计的摘要。
- 导入的 Agent 历史房保持只读；点击“从此历史继续”会创建普通可写房间，并绑定来源
  conversation ID、SHA-256 和有限上下文。新房间仍可修改 Seat、角色、路由和自动模式。
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
