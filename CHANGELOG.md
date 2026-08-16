# 更新日志

所有重要变更均记录在此。

## 未发布

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
