# 安全策略

## 支持版本

项目只支持最新公开版本，仅适用于 macOS。运行环境需要 Python 3.11 或更高版本，
并且必须安装已签名、能够通过安装器运行时能力检查的 Codex Computer Use。

## 报告安全漏洞

如果怀疑存在漏洞或凭据泄露，请勿创建公开 Issue。请使用本仓库的 GitHub 私密漏洞报告功能，
并提供受影响版本、复现步骤、影响范围，以及可选的缓解建议。

报告中不得包含真实飞书 Webhook、签名密钥、Keychain 值、Transcript 或 Codex 私有数据。
如果凭据已经暴露，请先撤销或轮换凭据。

## 信任边界

codex-notify 会修改当前用户的 Codex Hooks、Computer Use 通知链、LaunchAgent、
Keychain 条目和私有运行目录。当所有权、签名、命令结构或持久化状态发生漂移时，
安装器会按失败关闭原则停止操作。Hook 命令仍须在 Codex 中人工审查并明确设为信任。
