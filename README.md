# codex-notify

一个面向 macOS 的轻量旁路通知工具：使用 Codex 官方 Hook 观察 Turn 启动，结合 `agent-turn-complete` 与一次性只读 App Server 查询校准 Turn 终态，通过可靠的 SQLite 发件队列将消息发送到飞书自定义机器人。

它不接管 Codex 客户端，不启动受控 Turn，不读取对话记录或 Codex 私有数据库，也不根据提示词内容猜测 Turn 来源。

本项目由社区独立维护，不是 OpenAI 或飞书官方产品。

## 文档导航

- [更新日志](https://github.com/guangcodes/codex-notify/blob/main/CHANGELOG.md)：已发布版本与未发布变更。
- [GitHub Releases](https://github.com/guangcodes/codex-notify/releases)：每个 tag 的发布说明和构建产物。
- [安全策略](https://github.com/guangcodes/codex-notify/blob/main/SECURITY.md)：支持版本、私密漏洞报告入口和信任边界。
- [参与贡献](https://github.com/guangcodes/codex-notify/blob/main/CONTRIBUTING.md)：支持契约、验证要求和发布文档同步规则。
- [第三方与商标声明](https://github.com/guangcodes/codex-notify/blob/main/THIRD_PARTY_NOTICES.md)：发行包、外部依赖、服务和商标边界。
- [实验通知覆盖](https://github.com/guangcodes/codex-notify/blob/main/docs/experimental-notification-coverage.md)：尽力通知、只读状态查询和真实环境验收的证据边界。
- [v0.1.0 发布验收记录](https://github.com/guangcodes/codex-notify/blob/main/docs/release-acceptance-v0.1.0.md)：首个公开版本的历史实机与产物证据；不代表后续版本的当前行为。

## 事件链

```text
SessionStart ────────────────→ 只记录会话生命周期和上下文压缩来源
UserPromptSubmit ────────────→ PENDING_ROOT_CANDIDATE（统一等待 5 秒）
PermissionRequest ───────────→ 当前不安装；旧版兼容入口静默
PreToolUse(request_user_input) → 仅精确归属已确认根 Turn 后登记尽力问题提醒
SubagentStart/SubagentStop ──→ 保存原始 Hook 身份并推导唯一活动父 Turn
agent-turn-complete ─────────→ 权威正常完成信号，触发有界终态校准
                                  ↓
                              一次性 metadata-only App Server 查询
                                  ↓
                      completed / failed（interrupted 静默）
                                  ↗
LaunchAgent 补偿扫描 ────────→ 只查询已登记且未终态的精确根 Thread/Turn
                                  ↓
                              根终态等待 5 秒并合并已确认子结果
                                  ↓
                              SQLite 发件队列
                                  ↓
                              LaunchAgent 后台进程
                                  ↓
                              飞书机器人

worker 独立低频支线：
mcpServerStatus/list ────────→ MCP 登录全局状态（实验，默认关闭）
account/rateLimits/read ─────→ 账户限流全局状态（实验，默认关闭）
```

## Turn 策略

状态分为四个互不替代的维度：

- `classification`：`PENDING_ROOT_CANDIDATE`、`NOTIFIABLE_ROOT`、`CONFIRMED_CHILD`、`UNVERIFIED`、`CONFLICT`
- `lifecycle`：`RUNNING`、`COMPLETED`
- `terminal_status`：仅持久化可通知的 `completed`、`failed`；App Server 的 `interrupted` 观测不可靠，保持静默并继续等待
- 抑制状态：`suppressed` 与可空的 `suppression_reason`

所有具有有效 `session_id + turn_id` 的 `UserPromptSubmit` 都先进入相同的 5 秒窗口。异步 worker 只使用 ChatGPT Desktop 内置的 Codex 启动一次性 App Server：优先调用 `thread/read(includeTurns=false)`；当 Hook `session_id` 不能直接读取时，使用 `thread/list(useStateDbOnly=true)` 在交互 Thread 中做唯一 ID 候选映射，再用 `thread/read(includeTurns=false)` 校准，并通过 `thread/turns/list(itemsView="notLoaded")` 确认 Hook `turn_id` 确实属于该 Thread。代码仅解析身份、来源和时间元数据；Turn Items 必须为空，列表中的 `preview` 不会保存或转发。只有精确 Turn 归属成立、`parentThreadId=null` 且来源为 `vscode`、`appServer` 或 `cli` 时才转为 `NOTIFIABLE_ROOT`；查询缺失、失败、超时、字段未知或证据冲突时保持静默。

`SubagentStart` 的 Hook `turn_id` 只按原始字段保存，不直接当作父 Turn。仅当父 Thread 当时恰好有一个运行中 Turn，且后续子事件的 Thread ID 精确等于 `agent_id`、Turn ID 精确等于该 Hook `turn_id`、时间顺序成立且关系无冲突时，才确认 `CONFIRMED_CHILD`。单凭时间、`agent_id` 或父 Thread 均不足以合并。

完成处理只使用精确的 `(session/thread id, turn_id)`，永不按相同 `turn_id` 跨会话回退。`UNKNOWN`、`UNVERIFIED` 和 `CONFLICT` 均不通知、不合并，也不为缺少已确认来源的完成事件发送独立通知。

`PermissionRequest` 不能区分自动审查、人工等待、批准、拒绝或已经失效，因而不足以证明“Codex 正在等待审批”。当前版本不安装该 Hook；旧版兼容入口静默且不写入 SQLite，升级时会移除旧 Hook 并抑制历史未发送项。`status` 中的审批计数仅为旧版本记录。

后台 worker 只对已确认并登记的根 Turn 调用 `thread/turns/list(itemsView="notLoaded")`。响应必须保持 `itemsView="notLoaded"` 且 `items` 为空；只提取身份、状态、时间、耗时和封闭集合内的错误类别。仅带完成时间的 `completed`、`failed` 可成为通知终态；`interrupted` 可能只是用户追加消息造成的瞬时状态，因此始终静默。`agent-turn-complete` 到达后先进行有界校准，不可读时兼容回退为 completed；缺少正常完成信号时每轮最多补偿查询一个候选，失败退避并在 24 小时后停止。

根 Turn 完成后等待固定 5 秒，以父 Turn 的 `last-assistant-message` 为主结果，并按 `SubagentStart.started_at` 合并已完成的确认子结果。子结果使用确定性脱敏与截断，最多 8 项、每项最多 200 字；窗口后到达的结果不补发。整个过程不调用模型生成摘要。

## 开关

- `codex-notify on`：允许新候选在证据确认后生成根启动和配对终态事件。
- `codex-notify off`：阻止新的启动事件；已经生成启动事件的 Turn 仍可发送配对完成。
- `codex-notify off --now`：在发送锁内等待当前投递结束，然后永久抑制运行中 Turn、待校准 Turn、pending 候选和未发送队列。重新 `on` 不会恢复这些 Turn。
- `codex-notify test`：显式测试操作，不受 Turn 分类和 `off --now` 影响。

尽力通知使用独立实验开关，升级后全部默认关闭：

```bash
codex-notify experimental status
codex-notify experimental enable request-user-input
codex-notify experimental disable request-user-input
codex-notify experimental enable mcp-auth
codex-notify experimental disable mcp-auth
codex-notify experimental enable rate-limits
codex-notify experimental disable rate-limits
```

总开关仍是最终门：`off` 后不登记新的实验通知或运行实验查询；`off --now` 还会永久抑制
当前实验状态和未发送事件。实验功能不会随 `on` 自动开启。capability 探测失败的功能显示
`unavailable`，不能启用。单独 disable 某项实验功能会永久抑制该功能尚未发送的事件，
不会影响另外两项实验功能或总开关。

Turn 级事件使用等价于以下元组的唯一键：

```text
(session_id, turn_id, "started")
(session_id, turn_id, "completed")
(session_id, turn_id, "request-user-input:<hook-payload-fingerprint>")
```

全局 MCP 与账户限流事件则使用安全哈希信号键、状态转换或窗口冷却身份，不伪造 Turn ID。SQLite 发件队列保证本地幂等、启动/完成顺序和失败重试。未发送项超过 24 小时后会标记为永久失败；消息在落库前会脱敏和截断。

## 依赖与兼容性

codex-notify 只支持当前 macOS 用户级部署，不支持 Linux、Windows、容器或无图形桌面的服务器安装。

| 类型 | 依赖 | 要求与用途 |
| --- | --- | --- |
| 操作系统 | macOS | 使用当前用户的 Keychain 和 `launchd`/LaunchAgent；集成安装器只写当前用户目录，不调用 `sudo`。 |
| Python | Python 3.11、3.12 或 3.13 | 包元数据允许 Python 3.11 及更高版本安装；发布 CI 实际覆盖 3.11–3.13。更高版本需单独验证。私有 runtime 不依赖安装时的 venv、pipx 环境或源码目录。 |
| Codex 集成 | Codex Computer Use | 强制外部依赖。必须已安装并启用，保持顶层 `notify` 所有权，并通过签名身份和 `--previous-notify` 能力检查。项目不固定锁死 Computer Use 版本号。 |
| 消息服务 | 启用签名校验的飞书自定义机器人 | 用户需要准备 Webhook URL 和签名密钥；当前不支持其他机器人或消息平台。 |
| Python 运行依赖 | 无 | `pyproject.toml` 的 `dependencies` 为空，运行时代码只使用 Python 标准库。`setuptools>=77` 仅用于构建发行包。 |

Computer Use、Codex、飞书及其服务不随本项目分发，分别受其提供方的许可、账号和服务条款约束。项目发行包只包含 codex-notify 自身源码和许可证，详见[第三方与商标声明](https://github.com/guangcodes/codex-notify/blob/main/THIRD_PARTY_NOTICES.md)。

## 安装、部署与配置

### 1. 安装 Python 包

推荐使用 [pipx](https://pipx.pypa.io/) 从 PyPI 安装 CLI，使它与系统 Python 和其他项目环境隔离：

```bash
pipx install --python python3.13 codex-notify
```

示例使用 Python 3.13；也可以将 `python3.13` 替换为本机可用的 Python 3.11 或 3.12。

也可以在已激活的虚拟环境中使用 pip：

```bash
python3 -m pip install codex-notify
```

从源码安装时同样建议使用 pipx 或已激活的虚拟环境；后续集成生命周期与 PyPI 包相同：

```bash
git clone https://github.com/guangcodes/codex-notify.git
cd codex-notify
pipx install --python python3.13 .
```

`pipx install` 或 `pip install` 只安装由包管理器拥有的 `codex-notify` 命令，不会修改 Codex 配置或 macOS 服务。

### 2. 部署用户级集成

确认 Computer Use 已安装并启用，然后执行：

```bash
codex-notify install
```

`install` 同时用于首次部署、重复安装和版本升级。它会进行所有权与能力预检，然后原子发布：

- `~/.codex/codex-notify/lib/`：不依赖 venv 或源码目录的私有 runtime。
- `~/.codex/codex-notify/runner.py`：Hook、通知链和 LaunchAgent 使用的私有入口。
- `~/.codex/hooks.json`：`SessionStart`、`UserPromptSubmit`、`SubagentStart`、`SubagentStop`，以及精确匹配 `request_user_input` 的 `PreToolUse` Hook；升级时移除旧版自有 `PermissionRequest` Hook。
- `~/.codex/config.toml`：保留 Computer Use 顶层 `notify`，只写入指向私有 runner 的 `--previous-notify`。
- `~/Library/LaunchAgents/io.github.guangcodes.codex-notify.plist`：每 10 秒处理一次发件队列的当前用户后台任务。

安装器使用操作锁、并发漂移检查和失败回滚；遇到未知通知链、文件所有权不明、符号链接或配置漂移时会停止，不猜测覆盖。

### 3. 配置飞书凭据

在飞书自定义机器人的安全设置中启用签名校验，准备好完整 Webhook URL 和签名密钥，然后执行：

```bash
codex-notify configure
```

命令会分别交互提示 `飞书机器人 Webhook URL` 和 `飞书机器人签名密钥`。输入不会出现在命令参数中；校验通过后，两项凭据会作为一个条目保存到当前用户的 macOS Keychain，不写入配置文件、SQLite 或日志。重新执行 `configure` 可更新现有凭据。

### 4. 重启 Codex 并信任 Hook

部署后必须重启 Codex，在 `/hooks` 中逐项检查并信任五个 Hook。自动安装和测试不能代替这项人工授权。

### 5. 验证并启用通知

```bash
codex-notify doctor
codex-notify test
codex-notify on
codex-notify status
```

- `doctor` 应确认凭据、五个 Hook 的命令、matcher 与元数据、bundled App Server 终态 schema、三个实验 capability、Computer Use 通知链、runtime 版本和 LaunchAgent；Hook 是否已被用户信任仍以 Codex `/hooks` 为准。
- `test` 应向飞书发送一条显式测试消息。
- `on` 允许后续符合策略的 Turn 生成通知；项目默认关闭，不会因安装自动开启。
- `status` 显示当前开关、实验 capability 与子开关、等待终态校准数、三类终态统计、历史审批与 best-effort 通知统计、最近一次实验状态成功查询时间和终态 App Server 查询结果、队列和投递概况；不会打印原始错误、命令或路径。

如果验证失败，不要反复覆盖配置；先根据 `doctor` 的具体失败项检查 Computer Use、Hook 信任、Keychain 或 LaunchAgent。

## 升级与旧版本迁移

如果从曾经创建 managed shim 的开发版升级，先确认 `command -v codex-notify` 指向当前 pip 或 pipx 的 console entry point；无法确认时使用与 `pip` 相同的 Python 解释器执行一次迁移安装，避免旧 shim 截获命令：

```bash
python3 -m codex_notify.cli install
```

迁移完成后统一使用 `codex-notify`。如果最初从 PyPI 使用 pipx 安装，后续升级执行：

```bash
pipx upgrade codex-notify
codex-notify install
```

如果使用虚拟环境中的 pip，则将第一条替换为：

```bash
python3 -m pip install --upgrade codex-notify
```

如果使用 `pipx install .` 从源码 checkout 安装，pipx 会记录该 checkout 的绝对路径；升级前
必须先刷新源码，再按原始来源重建 pipx 环境：

```bash
git pull --ff-only
pipx reinstall --python python3.13 codex-notify
codex-notify install
```

若源码目录已删除或希望改回 PyPI 发行包，应重新建立来源：

```bash
pipx uninstall codex-notify
pipx install --python python3.13 codex-notify
codex-notify install
```

升级 Python 包后必须再次执行 `codex-notify install`，把同版本 runtime 发布到私有运行目录。Computer Use 必须保持顶层 `notify` 所有权，codex-notify 只通过其原生 `--previous-notify` 链式能力接入。安装器接受签名身份匹配且能力探测通过的 Computer Use 版本；签名异常版本只接受内置的精确摘要白名单，其他情况失败关闭。

安装器使用当前用户的主目录，不依赖固定用户名、固定主目录、私有 Skill、虚拟环境或第三方提示词模板。升级时会精确移除旧版归属的 Hook、CLI shim 和 LaunchAgent，保留其他程序的配置。

## 卸载

```bash
codex-notify uninstall
codex-notify uninstall --purge
```

普通卸载精确移除本项目拥有的 Hook、Computer Use 通知链、LaunchAgent 和私有运行环境，同时保留 SQLite 数据与日志。它不会删除由 pip 或 pipx 管理的 `codex-notify` 命令。旧数据库中的废弃兼容数据保持不活动状态，不会在普通启动或卸载时执行破坏性的 `DROP`。`--purge` 按既有语义删除全部运行数据。

应先卸载集成，再按最初的包管理方式卸载 Python 包。使用 pipx 安装时执行：

```bash
codex-notify uninstall
pipx uninstall codex-notify
```

使用已激活虚拟环境中的 pip 安装时执行：

```bash
codex-notify uninstall
python3 -m pip uninstall codex-notify
```

若已经误删 Python 包，私有 runner 仍提供救援卸载：

```bash
python3 ~/.codex/codex-notify/runner.py uninstall
```

卸载器不会修改或删除 Computer Use App。若配置发生未知漂移、目标是符号链接、通知链所有权不明确或 LaunchAgent 状态无法确认，操作会在破坏性步骤前停止。

## 隐私与本地数据

发送到飞书的确定通知包含项目名称、事件类型、confirmed 标识、安全摘要、时间、耗时、短 Turn ID 和事件 ID；尽力通知明确包含 best_effort、signal_source、安全信号 ID 和“可能/建议检查”文案。全局 MCP/账户状态不伪造 Turn ID。失败通知最多包含封闭集合内的结构化错误类别。摘要在进入 SQLite 前会将命中的凭据和本地路径分别替换为 `[敏感信息已打码]` 与 `[本地路径已打码]`，保留其余任务上下文后再执行长度截断；正则脱敏不能保证识别所有业务机密，不要在高敏感项目中启用通知。App Server 原始响应、preview、Prompt、Turn Items、request_user_input 问题与选项、MCP tools/resources/schema、OAuth URL、reset credit、完整命令、完整路径、工具原始参数、`error.additionalDetails`、环境变量值、凭据和 Token 不会保存或转发。

飞书 Webhook 和签名密钥只保存在当前用户的 macOS Keychain。运行数据位于 `~/.codex/codex-notify/`，SQLite 与日志仅对当前用户开放。未发送项最多重试 24 小时，超过期限后在 SQLite 中标记为永久失败；飞书已接收而本地未收到确认时，重试可能产生相同事件 ID 的重复消息。普通卸载保留 SQLite 与日志，只有 `uninstall --purge` 删除运行数据。

## 已知边界

- App Server 补偿扫描只能在精确登记范围内确认带完成时间的 `completed`、`failed`；`PermissionRequest` 和 `interrupted` 都缺少权威、稳定的待处理或终止证据，当前不通知。
- 父子关系是受约束推导，只优化能够由唯一活动父 Turn 和精确子身份共同确认的情况；无法确认时宁可静默。
- metadata-only 校准依赖 ChatGPT Desktop bundled Codex 的实验性 App Server 契约；缺失、漂移或失败只会降低通知覆盖率，不影响 Codex Desktop。
- `request_user_input`、MCP 登录状态和账户限流是默认关闭的 best-effort 实验能力；mock/schema 验证不等于真实环境已经触发或无副作用。
- MCP form/URL elicitation、Connector 确认、model verification、直接 OAuth/重新认证、外部页面操作、验证码和 MFA 仅存在原 host 实时事件或没有安全只读信号，当前不可观察。完整证据矩阵见 [实验通知覆盖](docs/experimental-notification-coverage.md)。
- `SessionStart source=compact` 只记录会话信息，不建立父子边。
- 飞书若已经接收请求但本地未收到确认，重试可能产生带相同事件 ID 的重复消息。
- 当前实现依赖 macOS Keychain 和 LaunchAgent。

官方契约说明见 [Codex Hooks](https://learn.chatgpt.com/docs/hooks)、
[Codex Notifications](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications) 和
[Codex App Server](https://learn.chatgpt.com/docs/app-server)。

## 许可与第三方声明

本项目依据 [MIT License](https://github.com/guangcodes/codex-notify/blob/main/LICENSE) 开源。外部软件、服务、依赖和商标边界见[第三方与商标声明](https://github.com/guangcodes/codex-notify/blob/main/THIRD_PARTY_NOTICES.md)。
