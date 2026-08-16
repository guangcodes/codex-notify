# 实验通知覆盖与证据边界

本文记录第二切片“尽力通知与实验性只读状态查询”的能力门。结论基于当前官方
[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、
[Codex App Server](https://learn.chatgpt.com/docs/app-server) 和目标 Mac bundled Codex
生成的 experimental JSON schema。初始实现门控没有连接真实 MCP、调用 OAuth、消费额度
或发送真实飞书消息；目标 Mac 的后续本地验收单独记录在本文末尾。

## 统一安全边界

- 所有新增通知均为 `certainty=best_effort`；确定的 PermissionRequest 和 Turn 终态不依赖实验开关。
- Hook 只校验、对本地幂等身份做 SHA-256 指纹并写 SQLite；不启动 App Server、访问 Keychain、发送网络请求或返回审批决定。
- worker 只在总开关和对应实验开关均开启时，按独立频率启动一次性 App Server 子进程。总超时 5 秒、stdout 上限 1 MiB，并与已有 metadata/terminal 查询共用非阻塞单实例锁。
- MCP 查询固定使用 `detail="toolsAndAuthOnly"`；tools schema 只存在于受限进程响应中，解析结果不会保存它。resources 与 resourceTemplates 必须为空，否则失败关闭。
- rate-limit parser 校验 `usedPercent` 为有限数值，但只保留服务端明确的 `rateLimitReachedType`、安全哈希状态键和窗口冷却哈希；不保存百分比、余额、套餐、组织、reset credit、Token 或账户标识。
- 原始 Hook payload、questions/options、MCP tools/resources、OAuth URL、App Server 原始响应、Prompt、preview、Turn Items、命令、路径和工具参数均不进入 SQLite、日志或消息。

## 能力矩阵

| 候选事件 | 公开协议形态 | 独立子进程能否观察原 Desktop Turn | 持久只读状态 / Turn 关联 | 结论 |
| --- | --- | --- | --- | --- |
| `request_user_input` | `PreToolUse` 可按本地函数 `tool_name` 精确正则匹配；App Server 另有实时 server request | Hook 可提供当前 `session_id + turn_id`；不依赖独立 App Server 实时连接 | 只接受已确认根 Turn，或精确关联到根的已确认子 Turn | **implemented / trusted-on-target-mac**：matcher `^request_user_input$`，通用文案，不读取问题正文；六个 Hook 已在目标 Mac 信任并激活，真实 `request_user_input` 触发仍待单独验收 |
| MCP 登录状态 | `mcpServerStatus/list` read API，`toolsAndAuthOnly`，结构化 `authStatus` | 不依赖原 Turn；按全局状态处理 | `notLoggedIn` 明确进入时一次，健康状态后允许未来再次通知 | **implemented / deferred-real-validation**：mock 与 schema 已验证；真实 MCP 是否会被初始化、是否完全无副作用仍待单独授权验收 |
| 账户限流 | `account/rateLimits/read` read API，结构化 `rateLimitReachedType` | 不依赖原 Turn；按账户级全局状态处理 | reached 转换通知；同窗口冷却；恢复后等待新的窗口身份 | **implemented / deferred-real-validation**：mock 与 schema 已验证；真实调用是否产生网络请求及账户行为仍待单独授权验收 |
| MCP form elicitation | `mcpServer/elicitation/request` server request | 否；只发给承载原 Turn 的 host 连接 | 可能有 thread/turn correlation，但没有安全的独立持久 read API | **unavailable** |
| MCP URL elicitation | `mcpServer/elicitation/request` server request | 否 | URL 与正文敏感，且没有独立持久 read API | **unavailable** |
| Connector 操作确认 | host 可能通过 `tool/requestUserInput` server request 发起 | 否；不能把普通 Connector/MCP tool call 猜成确认 | 没有本切片允许的独立持久 read API | **unavailable** |
| model verification | `model/verification` server notification | 否 | schema 有 Thread/Turn，但通知只属于原实时连接 | **unavailable** |
| OAuth / `reauthenticationRequired` | OAuth 是写/登录方法；startup failure 是 server notification；auth status 可读 | 实时通知不可见；全局 auth 状态可读 | 仅 MCP `notLoggedIn` 由上面的全局状态功能覆盖 | **unavailable**（直接事件）；MCP auth 状态为 implemented/deferred-real-validation |
| 外部页面操作 | 可能经 URL elicitation 或 host UI | 否 | 无安全的独立持久 read API | **unavailable** |
| 验证码 / MFA | 没有通过当前门控的公开 Hook 或只读状态 | 否 | 内容高度敏感，不读取聊天或表单正文 | **unavailable** |

“协议中存在”不等于新启动的 App Server 子进程会收到原 Desktop Turn 的 server request
或 notification。codex-notify 不恢复 Turn、不代理 Desktop 连接、不回复 server request，也不以
daemon 方式运行 App Server。

## 开关、状态和幂等

```text
codex-notify experimental status
codex-notify experimental enable request-user-input
codex-notify experimental enable mcp-auth
codex-notify experimental enable rate-limits
codex-notify experimental disable <feature>
```

实验开关保存在 SQLite，升级后默认关闭。总开关关闭时不登记新的实验通知，也不运行实验查询；
`off --now` 会抑制当前实验 outbox 和已观察状态，重新 `on` 不会恢复这些事件。
单独 disable 某项功能会在发送锁内永久抑制该功能的 pending、retry 或已 claim 事件，不影响
其他实验功能。

Turn 级信号依赖对应 started 事件成功发送。全局 MCP/账户状态使用空的 `session_id` 和
`turn_id`，不会伪造 Turn。查询失败不会覆盖最后一次成功状态，也不会制造恢复或新转换。

## 当前验证边界

本地自动化证明 mock Hook、mock App Server、schema 兼容、SQLite 迁移、状态转换、幂等、
冷却、安装/卸载和失败开放。它不证明真实 Hook 会触发、Hook 已被用户信任、真实 MCP 查询
无副作用、rate-limit read 不联网，或真实飞书已经收到实验通知。

## 目标 Mac v0.1.3 发布前验收（2026-08-16）

- Python 3.13 pipx 环境、CLI 和私有 runtime 均安装为 `0.1.3`；LaunchAgent 已加载。
- `codex-notify doctor` 通过，三个实验 capability 均为 `available` 且保持默认关闭。
- Codex `/hooks` 已逐项核对：`SessionStart`、`UserPromptSubmit`、`SubagentStart`、
  `SubagentStop`、`PermissionRequest` 和精确 matcher 的 `PreToolUse` 均为 active。
- `codex-notify test` 已向现有飞书机器人成功发送显式测试消息；随后队列待发送/重试和永久失败均为 0。
- 这组证据证明本地安装、Hook 信任、通知链和普通飞书投递可用；未启用或触发三个实验功能，
  因而不证明真实 `request_user_input`、MCP 登录状态或账户限流通知已经端到端送达。
- 本节记录的是发布前本地源码安装，不等同于 GitHub Release、PyPI 产物或发布后全新环境安装证据。
