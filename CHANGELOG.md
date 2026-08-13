# 更新日志

所有重要变更均记录在此。

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
