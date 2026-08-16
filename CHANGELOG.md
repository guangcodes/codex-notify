# 更新日志

所有重要变更均记录在此。

## 未发布

- 新增默认关闭的 best-effort 实验通知：精确 `request_user_input` PreToolUse、MCP `notLoggedIn` 全局状态和账户 `rateLimitReachedType` 全局状态。
- 新增 `codex-notify experimental status|enable|disable`，总开关保持权威；`off --now` 永久抑制当前实验 outbox 和已观察状态。
- SQLite 升级到 v8，增加实验 capability、可信度、信号来源/类型、安全信号 ID、状态转换、冷却和最近成功查询状态；迁移保持可重复且不重放历史事件。
- 增加一次性受限实验 App Server 查询：共享非阻塞锁、5 秒超时、1 MiB 输出上限、独立频率限制、严格 schema/pagination 校验；实验失败不阻塞终态扫描。
- 安装器新增精确 `^request_user_input$` PreToolUse matcher，并安全迁移旧版自有 Bash PreToolUse；doctor/status 显示实验 capability、开关和 best_effort 统计。
- 新增实验能力证据矩阵，明确 MCP elicitation、Connector 确认、model verification、直接 OAuth/重新认证、外部页面、验证码与 MFA 当前不可观察。

- 新增确定的 `PermissionRequest` 审批通知：严格根/子 Turn 归属、失败关闭、稳定幂等、通用操作分类和入库前脱敏；Hook 永不返回 allow/deny，也不保存原始 payload。
- 新增一次性只读 `thread/turns/list(itemsView="notLoaded")` 终态读取，校准 `completed`、`failed`、`interrupted`，拒绝非空 Items、未知状态/字段、超时、超大输出和身份冲突。
- 新增有界终态补偿扫描、查询退避、24 小时停止、App Server 单实例锁，以及 `agent-turn-complete` 校准窗口后的 completed 兼容回退。
- SQLite 升级到 v7，增量保存精确 App Thread ID、终态、结构化错误类别、查询调度和诊断字段；旧 completed 数据可重复迁移且不重放历史通知。
- 扩展终态与审批消息、`status` 统计和 `doctor` schema 检查；安装器幂等管理 PermissionRequest Hook，普通卸载仍保留数据库和日志。
- 避免脱敏测试中的 Stripe 和 Slack 假凭据以连续字面量出现，防止 Secret scanning 误报。
- 补齐版本、安全、贡献、第三方依赖和历史发布验收文档之间的入口与事实边界。

## 0.1.2（2026-08-15）

- 增加一次性 metadata-only App Server 校准，只在精确确认根 Thread、Turn 和来源后通知。
- 收紧父子 Turn 识别：要求唯一活动父 Turn、精确子 Thread/Turn 身份、正确时间顺序且无冲突。
- 将已确认子 Turn 的结构化结果合并到根完成通知，最多 8 项、每项最多 200 字。
- 移除跨会话 `turn_id` 回退和缺少已确认来源时的独立完成通知，未知或冲突证据保持静默。
- 明确 App Server 响应原文、preview、prompt、cwd 和消息 Item 不会保存或转发；只保留关系判断所需的身份、来源和时间元数据。

发布说明：[GitHub Release v0.1.2](https://github.com/guangcodes/codex-notify/releases/tag/v0.1.2)；
完整差异：[v0.1.1...v0.1.2](https://github.com/guangcodes/codex-notify/compare/v0.1.1...v0.1.2)。

## 0.1.1（2026-08-13）

- 增加 wheel 和 sdist 的发行内容白名单、敏感路径与本地运行产物检查。
- 增加 wheel 与全新源码安装的生命周期一致性验证。
- 在发布后自动比对本次构建、GitHub Release 与 PyPI 文件的 SHA-256。
- 完善中文安装、升级、授权、依赖、隐私和第三方权利说明。

## 0.1.0（2026-08-13）

- 提供由 Python 包管理的 `codex-notify install` 和 `uninstall` 生命周期。
- 提供自包含的 macOS 私有运行环境、LaunchAgent、SQLite 发件队列和 Keychain 存储。
- 通过 Computer Use `--previous-notify` 接入，并执行严格的所有权检查。
- 基于公开 Codex Hook 对 Turn 启动和完成事件进行分类。
- 支持飞书消息脱敏、失败重试、保留期限和重复事件 ID。
- 支持旧 Hook、安装状态、CLI shim 和 LaunchAgent 的安全迁移。

各版本的 GitHub 发布说明见 [Releases](https://github.com/guangcodes/codex-notify/releases)。
