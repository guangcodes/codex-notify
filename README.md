# codex-notify

一个面向 macOS 的轻量旁路通知工具：使用 Codex 官方 Hook 观察 Turn 开始，使用官方 `agent-turn-complete` 通知观察真实完成，通过可靠的 SQLite 发件队列将消息发送到飞书自定义机器人。

它不接管 Codex 客户端，不启动受控 Turn，不读取对话记录或 Codex 私有数据库，也不根据提示词内容猜测 Turn 来源。

本项目由社区独立维护，不是 OpenAI 或飞书官方产品。

## 为什么需要 codex-notify

Codex 执行长时间任务时，用户可能已经离开电脑。任务完成后，如果没有主动提醒，用户往往不能及时回来确认结果或推进下一步工作。

ChatGPT Remote 可以用于远程查看和操作电脑上的任务；codex-notify 则补充移动端主动通知能力。当用户不在电脑旁时，它会通过飞书发送 Codex Turn 的启动和完成消息，让用户及时了解任务进度并响应后续工作。

codex-notify 不提供远程控制能力，也不替代 ChatGPT Remote。它只负责可靠地把任务状态推送到用户已有的移动消息渠道。

## 事件链

```text
SessionStart ────────────────→ 只记录会话生命周期和上下文压缩来源
UserPromptSubmit ────────────→ PENDING_ROOT_CANDIDATE（统一等待 5 秒）
SubagentStart/SubagentStop ──→ 保存原始 Hook 身份并推导唯一活动父 Turn
agent-turn-complete ─────────→ 唯一权威完成事件
                                  ↓
                              一次性 metadata-only App Server 校准
                                  ↓
                              根完成等待 5 秒并合并已确认子结果
                                  ↓
                              SQLite 发件队列
                                  ↓
                              LaunchAgent 后台进程
                                  ↓
                              飞书机器人
```

## Turn 策略

状态分为三个互不替代的维度：

- `classification`：`PENDING_ROOT_CANDIDATE`、`NOTIFIABLE_ROOT`、`CONFIRMED_CHILD`、`UNVERIFIED`、`CONFLICT`
- `lifecycle`：`RUNNING`、`COMPLETED`
- 抑制状态：`suppressed` 与可空的 `suppression_reason`

所有具有有效 `session_id + turn_id` 的 `UserPromptSubmit` 都先进入相同的 5 秒窗口。异步 worker 只使用 ChatGPT Desktop 内置的 Codex 启动一次性 App Server：优先调用 `thread/read(includeTurns=false)`；当 Hook `session_id` 不能直接读取时，使用 `thread/list(useStateDbOnly=true)` 在交互 Thread 中做唯一 ID 候选映射，再用 `thread/read(includeTurns=false)` 校准，并通过 `thread/turns/list(itemsView="notLoaded")` 确认 Hook `turn_id` 确实属于该 Thread。代码仅解析身份、来源和时间元数据；Turn Items 必须为空，列表中的 `preview` 不会保存或转发。只有精确 Turn 归属成立、`parentThreadId=null` 且来源为 `vscode`、`appServer` 或 `cli` 时才转为 `NOTIFIABLE_ROOT`；查询缺失、失败、超时、字段未知或证据冲突时保持静默。

`SubagentStart` 的 Hook `turn_id` 只按原始字段保存，不直接当作父 Turn。仅当父 Thread 当时恰好有一个运行中 Turn，且后续子事件的 Thread ID 精确等于 `agent_id`、Turn ID 精确等于该 Hook `turn_id`、时间顺序成立且关系无冲突时，才确认 `CONFIRMED_CHILD`。单凭时间、`agent_id` 或父 Thread 均不足以合并。

完成处理只使用精确的 `(session/thread id, turn_id)`，永不按相同 `turn_id` 跨会话回退。`UNKNOWN`、`UNVERIFIED` 和 `CONFLICT` 均不通知、不合并，也不为缺少已确认来源的完成事件发送独立通知。

根 Turn 完成后等待固定 5 秒，以父 Turn 的 `last-assistant-message` 为主结果，并按 `SubagentStart.started_at` 合并已完成的确认子结果。子结果使用确定性脱敏与截断，最多 8 项、每项最多 200 字；窗口后到达的结果不补发。整个过程不调用模型生成摘要。

## 开关

- `codex-notify on`：允许新候选在证据确认后生成根启动事件和配对完成事件。
- `codex-notify off`：阻止新的启动事件；已经生成启动事件的 Turn 仍可发送配对完成。
- `codex-notify off --now`：在发送锁内等待当前投递结束，然后永久抑制运行中 Turn、pending 候选和未发送队列。重新 `on` 不会恢复这些 Turn。
- `codex-notify test`：显式测试操作，不受 Turn 分类和 `off --now` 影响。

启动和完成事件分别使用等价于以下元组的唯一键：

```text
(session_id, turn_id, "started")
(session_id, turn_id, "completed")
```

SQLite 发件队列保证本地幂等、启动/完成顺序、失败重试和 24 小时保留边界。消息在落库前会脱敏和截断。

## 依赖与兼容性

codex-notify 只支持当前 macOS 用户级部署，不支持 Linux、Windows、容器或无图形桌面的服务器安装。

| 类型 | 依赖 | 要求与用途 |
| --- | --- | --- |
| 操作系统 | macOS | 使用当前用户的 Keychain 和 `launchd`/LaunchAgent；集成安装器只写当前用户目录，不调用 `sudo`。 |
| Python | Python 3.11 或更高版本 | 用于安装 CLI；私有 runtime 会绑定一个可长期使用的基础解释器，不依赖安装时的 venv、pipx 环境或源码目录。 |
| Codex 集成 | Codex Computer Use | 强制外部依赖。必须已安装并启用，保持顶层 `notify` 所有权，并通过签名身份和 `--previous-notify` 能力检查。项目不固定锁死 Computer Use 版本号。 |
| 消息服务 | 启用签名校验的飞书自定义机器人 | 用户需要准备 Webhook URL 和签名密钥；当前不支持其他机器人或消息平台。 |
| Python 运行依赖 | 无 | `pyproject.toml` 的 `dependencies` 为空，运行时代码只使用 Python 标准库。`setuptools>=77` 仅用于构建发行包。 |

Computer Use、Codex、飞书及其服务不随本项目分发，分别受其提供方的许可、账号和服务条款约束。项目发行包只包含 codex-notify 自身源码和许可证，详见[第三方与商标声明](THIRD_PARTY_NOTICES.md)。

## 安装、部署与配置

### 1. 安装 Python 包

推荐从 PyPI 安装：

```bash
python3 -m pip install codex-notify
```

也可以从源码安装，后续生命周期与 PyPI 包完全相同：

```bash
git clone https://github.com/guangcodes/codex-notify.git
cd codex-notify
python3 -m pip install .
```

`pip install` 只安装由包管理器拥有的 `codex-notify` 命令，不会修改 Codex 配置或 macOS 服务。

### 2. 部署用户级集成

确认 Computer Use 已安装并启用，然后执行：

```bash
codex-notify install
```

`install` 同时用于首次部署、重复安装和版本升级。它会进行所有权与能力预检，然后原子发布：

- `~/.codex/codex-notify/lib/`：不依赖 venv 或源码目录的私有 runtime。
- `~/.codex/codex-notify/runner.py`：Hook、通知链和 LaunchAgent 使用的私有入口。
- `~/.codex/hooks.json`：`SessionStart`、`UserPromptSubmit`、`SubagentStart`、`SubagentStop` Hook。
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

部署后必须重启 Codex，在 `/hooks` 中逐项检查并信任四个 Hook。自动安装和测试不能代替这项人工授权。

### 5. 验证并启用通知

```bash
codex-notify doctor
codex-notify test
codex-notify on
codex-notify status
```

- `doctor` 应确认凭据、四个 Hook 的命令与元数据、Computer Use 通知链、runtime 版本和 LaunchAgent；Hook 是否已被用户信任仍以 Codex `/hooks` 为准。
- `test` 应向飞书发送一条显式测试消息。
- `on` 允许后续符合策略的 Turn 生成通知；项目默认关闭，不会因安装自动开启。
- `status` 显示当前开关、运行中 Turn、待发送/重试队列和最近投递结果。

如果验证失败，不要反复覆盖配置；先根据 `doctor` 的具体失败项检查 Computer Use、Hook 信任、Keychain 或 LaunchAgent。

## 升级与旧版本迁移

如果从曾经创建 managed shim 的开发版升级，先确认 `command -v codex-notify` 指向当前 pip 或 pipx 的 console entry point；无法确认时使用与 `pip` 相同的 Python 解释器执行一次迁移安装，避免旧 shim 截获命令：

```bash
python3 -m codex_notify.cli install
```

迁移完成后统一使用 `codex-notify`；后续升级执行：

```bash
python3 -m pip install --upgrade codex-notify
codex-notify install
```

升级 Python 包后必须再次执行 `codex-notify install`，把同版本 runtime 发布到私有运行目录。Computer Use 必须保持顶层 `notify` 所有权，codex-notify 只通过其原生 `--previous-notify` 链式能力接入。安装器接受签名身份匹配且能力探测通过的 Computer Use 版本；签名异常版本只接受内置的精确摘要白名单，其他情况失败关闭。

安装器使用当前用户的主目录，不依赖固定用户名、固定主目录、私有 Skill、虚拟环境或第三方提示词模板。升级时会精确移除旧版归属的 Hook、CLI shim 和 LaunchAgent，保留其他程序的配置。

## 诊断与状态

```bash
codex-notify status
codex-notify doctor
```

`status` 显示开关、运行中 Turn、待分类候选、发件队列、永久失败和最近投递状态。

`doctor` 检查：

- macOS 与运行入口
- Keychain 凭据
- 四个当前 Hook 的精确命令和元数据
- Computer Use 身份及 `--previous-notify` 能力
- `install-state.json` 与通知链一致性
- 当前包、私有运行环境与安装状态版本一致性
- LaunchAgent 配置及加载状态
- 旧 LaunchAgent 是否残留

`doctor` 不检查已移除的启动器、HMAC 意图或私有审查器数据库结构。`/hooks` 的人工信任仍需单独确认。

## 卸载

```bash
codex-notify uninstall
codex-notify uninstall --purge
```

普通卸载精确移除本项目拥有的 Hook、Computer Use 通知链、LaunchAgent 和私有运行环境，同时保留 SQLite 数据与日志。它不会删除由 pip 或 pipx 管理的 `codex-notify` 命令。旧数据库中的废弃兼容数据保持不活动状态，不会在普通启动或卸载时执行破坏性的 `DROP`。`--purge` 按既有语义删除全部运行数据。

应先卸载集成，再卸载 Python 包：

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

发送到飞书的消息包含项目名称、用户任务摘要、父 Turn 最终结果、已确认子 Turn 的结构化结果、时间、耗时、短 Turn ID 和事件 ID。摘要在进入 SQLite 前经过敏感格式检测、整段替换和长度截断，但正则脱敏不能保证识别所有业务机密；不要在高敏感项目中启用通知。App Server 的响应、preview、prompt、cwd 和消息 Item 不会保存。

飞书 Webhook 和签名密钥只保存在当前用户的 macOS Keychain。运行数据位于 `~/.codex/codex-notify/`，SQLite 与日志仅对当前用户开放。发件队列最长保留 24 小时；飞书已接收而本地未收到确认时，重试可能产生相同事件 ID 的重复消息。普通卸载保留数据，只有 `--purge` 删除运行数据。

## 开发验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src scripts tests
git diff --check
python3 -m build
```

自动测试证明源码状态机、SQLite 迁移、安装器模拟、Computer Use 链、Keychain、发件队列、重试和并发规则；它不能证明真实 Hook 信任、真实用户/子 Turn、真实飞书投递、目标 Mac 生命周期或离线重试恢复。

`v0.1.0` 的发布产物、目标 Mac 集成和真实飞书链路证据见
[发布验收记录](docs/release-acceptance-v0.1.0.md)。记录会明确区分真实验证、自动化验证和未重新执行项。

## 已知边界

- `agent-turn-complete` 是唯一完成来源；没有真实完成事件时不会伪造完成。
- 父子关系是受约束推导，只优化能够由唯一活动父 Turn 和精确子身份共同确认的情况；无法确认时宁可静默。
- metadata-only 校准依赖 ChatGPT Desktop bundled Codex 的实验性 App Server 契约；缺失、漂移或失败只会降低通知覆盖率，不影响 Codex Desktop。
- `SessionStart source=compact` 只记录会话信息，不建立父子边。
- 飞书若已经接收请求但本地未收到确认，重试可能产生带相同事件 ID 的重复消息。
- 当前实现依赖 macOS Keychain 和 LaunchAgent。

官方字段说明见 [Codex Hooks](https://developers.openai.com/codex/config-advanced#hooks) 和 [Codex Notifications](https://developers.openai.com/codex/config-advanced#notifications)。

## 许可、版权与第三方权利

除非具体文件另有说明，本项目源码和随附文档的版权声明为：

```text
Copyright (c) 2026 guangcodes
```

项目依据 [MIT License](LICENSE) 开源。MIT 许可证允许使用、复制、修改、合并、发布、分发、再许可和销售软件副本，但所有副本或软件的重要部分必须保留原版权声明和许可声明。软件按“原样”提供，不附带任何明示或默示担保；完整且具有约束力的条款以英文 `LICENSE` 文件为准。

MIT 许可证仅适用于本仓库实际发布的 codex-notify 代码和文档，不会改变 Computer Use、Codex、飞书、GitHub、PyPI 或其他外部软件与服务的许可和服务条款。本项目由社区独立维护，不是 OpenAI 或飞书官方产品，也不表示相关权利人对本项目提供背书。

项目当前没有打包、复制或 vendoring 第三方运行时代码；构建、测试和外部服务边界见[第三方与商标声明](THIRD_PARTY_NOTICES.md)。OpenAI、Codex、飞书及文档中出现的其他产品名称、标识和商标归各自权利人所有。
