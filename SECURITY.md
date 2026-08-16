# 安全策略

## 支持版本

项目只为[最新公开版本](https://github.com/guangcodes/codex-notify/releases/latest)提供安全更新，
更早版本不受支持。升级 Python 包后必须重新执行 `codex-notify install`。

项目仅支持 macOS。Python、Codex Computer Use 和其他运行要求以
[README 的依赖与兼容性说明](https://github.com/guangcodes/codex-notify#依赖与兼容性)为准。

## 报告安全漏洞

如果怀疑存在漏洞或凭据泄露，请勿创建公开 Issue。请使用本仓库已启用的
[GitHub 私密漏洞报告功能](https://github.com/guangcodes/codex-notify/security/advisories/new)，
并提供受影响版本、复现步骤、影响范围，以及可选的缓解建议。

仓库当前已启用私密漏洞报告。若上述入口不可用，请勿改用公开 Issue 披露漏洞细节；等待
仓库管理员恢复私密报告入口。

报告中不得包含真实飞书 Webhook、签名密钥、Keychain 值、Transcript 或 Codex 私有数据。
如果凭据已经暴露，请先撤销或轮换凭据。

## 信任边界

codex-notify 会修改当前用户的 Codex Hooks、Computer Use 通知链、LaunchAgent、
Keychain 条目和私有运行目录。当所有权、签名、命令结构或持久化状态发生漂移时，
安装器会按失败关闭原则停止操作。Hook 命令仍须在 Codex 中人工审查并明确设为信任。
