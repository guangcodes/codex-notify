# v0.1.0 发布验收记录

验收日期：2026-08-13

发布版本：`codex-notify==0.1.0`
发布源码提交：`e885849561c8f6eb3364d00188a891d12c2ebbe4`

本文记录首个公开版本的实际证据边界。真实 Mac、自动测试和线上发布产物分别记录，
任何一类通过都不替代另一类。

## 发布产物

从 GitHub Release `v0.1.0` 重新下载两个资产后，已通过 PyPI JSON API 逐文件比较 SHA-256：

| 文件 | SHA-256 | GitHub Release 与 PyPI |
| --- | --- | --- |
| `codex_notify-0.1.0-py3-none-any.whl` | `df7f0bc82a09969ac8d2a58daff7bc05e22448df8dc7d14e8e3e2de517f12035` | 一致 |
| `codex_notify-0.1.0.tar.gz` | `bdc040cd94b07cc0f73efa09e14893fbf17d9202cea93cddf0b918b5adab427e` | 一致 |

后续发布工作流会在 Trusted Publisher 上传完成后自动执行同一检查；也可以手动运行
`Release integrity` workflow 复核任意 tag。仓库本地 `dist/` 可能来自另一次构建，
不能仅凭文件名视为已发布资产。

## 真实 Mac 集成

在当前 macOS 用户环境完成重启 Codex 后，使用正常用户上下文执行
`codex-notify doctor`，以下项目于 2026-08-13 全部通过：

- 安装入口、私有 runtime 和 `install-state.json` 均为 `0.1.0`。
- Keychain 飞书凭据可读取。
- `SessionStart`、`UserPromptSubmit`、`SubagentStart`、`SubagentStop` 四个 Hook 已安装；
  Hook 信任仍以 Codex `/hooks` 中的人工确认结果为准。
- Computer Use `26.804.1000633` 的签名身份、完整性和 `--previous-notify` 能力检查通过。
- 顶层 `notify` 仍由 Computer Use 拥有，`--previous-notify` 精确指向私有 runner。
- `io.github.guangcodes.codex-notify` LaunchAgent 已加载，旧 label 未残留。

此前在同一最终 runtime 源码上已观察到真实用户 Turn 的启动/完成飞书消息，以及真实 child
Turn 事件。公开事件无法稳定证明 child Turn 与父 Turn 的关联时，当前策略按设计失败开放，
允许其作为独立完成通知；这不是“已完全消除内部 Turn 误报”的证明。

最终 PyPI wheel 又在全新虚拟环境完成了 CLI 与安装器 smoke。发布前最后一次目标 Mac 集成
使用的 runtime 文件与发布源码一致，但 GitHub Actions/文档的后续提交不会重新部署 runtime；
因此这里分别陈述“目标 Mac 集成通过”和“线上 wheel 全新环境通过”，不把两者合并成
一条无法复现的证据。

## 自动验收

CI 对 macOS 和 Python 3.11、3.12、3.13 执行完整单元测试、`ResourceWarning`、
`compileall` 与敏感信息扫描。打包任务还执行：

- 解包检查 wheel 与 sdist 的路径白名单、缓存、运行数据、个人绝对路径和私有工具路径。
- 在两个全新 venv 中分别执行 `pip install <wheel>` 与从全新 clone 执行 `pip install .`。
- 两种来源各自完成 install、reinstall、普通 uninstall、再次 install 和 purge 生命周期。
- 对配置、Hook、安装状态、LaunchAgent、runner 和 runtime 清单生成规范化 JSON，逐字段比较。
- 发布后逐文件比较本次构建、GitHub Release 和 PyPI 的 SHA-256。

## 未由本记录重新证明的项目

以下行为有自动化回归覆盖，但没有在发布后的最终 PyPI wheel 上逐项重新进行真实破坏性演练：

- 断网入队后恢复网络的真实飞书重试。
- 普通卸载、救援卸载和 `--purge` 三条真实用户数据路径的连续演练。
- 旧版本原地升级的再次真实演练。

这些边界不影响已经记录的 `v0.1.0` 发布事实，但后续版本若把它们设为发布阻塞项，
必须在目标 Mac 上重新执行并追加独立记录，不能用模拟测试代替。
