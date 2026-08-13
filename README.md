# codex-notify

一个面向 macOS 的轻量旁路通知工具：使用 Codex 官方 Hook 观察 Turn 开始，使用官方 `agent-turn-complete` 通知观察真实完成，通过可靠的 SQLite 发件队列将消息发送到飞书自定义机器人。

它不接管 Codex 客户端，不启动受控 Turn，不读取对话记录或 Codex 私有数据库，也不根据提示词内容猜测 Turn 来源。

本项目由社区独立维护，不是 OpenAI 或飞书官方产品。

## 事件链

```text
SessionStart ────────────────→ 只记录会话生命周期和上下文压缩来源
UserPromptSubmit ────────────→ PENDING_ROOT_CANDIDATE（统一等待 5 秒）
SubagentStart/SubagentStop ──→ 只记录官方父 Turn → 子代理 agent_id 关系
agent-turn-complete ─────────→ 唯一权威完成事件
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

所有具有有效 `session_id + turn_id` 的 `UserPromptSubmit` 都先进入相同的 5 秒窗口。当前公开 Codex 契约没有把子代理 Hook 的 `agent_id` 连接到子 Turn 的 `UserPromptSubmit.session_id` 或 `agent-turn-complete.thread-id`，因此工具不会假设这些字段相等。窗口结束后，没有可直接证明的内部关系就按失败开放策略转为 `NOTIFIABLE_ROOT`。

`NOTIFIABLE_ROOT` 只表示“按照当前策略应该通知”，不表示已经证明提示词由用户直接输入。内部审查器、守护任务、上下文压缩或子代理 Turn 可能产生少量误报；这是“尽量不漏用户根 Turn”优先级下的明确取舍。

完成处理只使用精确的 `(session/thread id, turn_id)`，永不按相同 `turn_id` 跨会话回退。缺失可发送启动事件时，如果完成事件身份有效、通知已开启且没有明确内部证据，会发送独立完成通知，并标注“未观测到对应启动事件”。不会补发或伪造启动。

## 开关

- `codex-notify on`：允许新候选在 5 秒后生成启动事件，也允许合规的独立完成事件。
- `codex-notify off`：阻止新的启动事件；已经生成启动事件的 Turn 仍可发送配对完成。
- `codex-notify off --now`：在发送锁内等待当前投递结束，然后永久抑制运行中 Turn、pending 候选和未发送队列。重新 `on` 不会恢复这些 Turn。
- `codex-notify test`：显式测试操作，不受 Turn 分类和 `off --now` 影响。

启动和完成事件分别使用等价于以下元组的唯一键：

```text
(session_id, turn_id, "started")
(session_id, turn_id, "completed")
```

SQLite 发件队列保证本地幂等、启动/完成顺序、失败重试和 24 小时保留边界。消息在落库前会脱敏和截断。

## 安装

前置条件：macOS、Python 3.11+、已安装并启用 Codex Computer Use，以及一个启用签名校验的飞书自定义机器人。

从 PyPI 安装：

```bash
python3 -m pip install codex-notify
codex-notify install
codex-notify configure
codex-notify test
codex-notify on
```

从源码安装使用相同生命周期：

```bash
git clone https://github.com/guangcodes/codex-notify.git
cd codex-notify
python3 -m pip install .
codex-notify install
```

如果从曾经创建 managed shim 的开发版升级，先确认 `command -v codex-notify` 指向当前 pip 或 pipx 的 console entry point；无法确认时使用与 `pip` 相同的 Python 解释器执行一次迁移安装，避免旧 shim 截获命令：

```bash
python3 -m codex_notify.cli install
```

迁移完成后统一使用 `codex-notify`；后续升级执行：

```bash
python3 -m pip install --upgrade codex-notify
codex-notify install
```

`pip install` 只安装 Python 包，不修改 Codex 或 macOS 服务；`codex-notify install` 才会原子安装或升级集成。Computer Use 必须保持顶层 `notify` 所有权，codex-notify 只通过其原生 `--previous-notify` 链式能力接入。安装器接受签名身份匹配且能力探测通过的 Computer Use 版本；签名异常版本只接受内置的精确摘要白名单，其他情况失败关闭。

安装器使用当前用户的主目录，不依赖固定用户名、固定主目录、私有 Skill、虚拟环境或第三方提示词模板。它会：

- 预检 Computer Use 身份、能力、现有通知链和所有写入目标。
- 使用稳定的基础 Python 解释器。
- 原子安装 `SessionStart`、`UserPromptSubmit`、`SubagentStart`、`SubagentStop` Hook。
- 升级时精确移除旧版归属的 `PreToolUse` 和 `Stop` Hook，保留其他程序的 Hook。
- 使用操作锁、原子配置发布、并发漂移检测和失败回滚。
- 安装或重装 `io.github.guangcodes.codex-notify` LaunchAgent，每 10 秒处理一次发件队列。
- 将旧安装状态、旧 Hook、旧 CLI shim 和旧 LaunchAgent 安全迁移到当前格式。

安装后需要重启 Codex，并在 `/hooks` 中人工检查和信任四个 Hook。Hook 信任状态无法由自动测试代替。

飞书 Webhook 地址和签名密钥通过交互输入并原子保存到 macOS Keychain，不进入命令参数、配置文件或 SQLite。Keychain 读取使用 Python `-P` 安全路径，避免当前目录模块遮蔽。

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

发送到飞书的消息包含项目名称、用户任务摘要、Codex 结果摘要、时间、耗时、短 Turn ID 和事件 ID。摘要在进入 SQLite 前经过敏感格式检测、整段替换和长度截断，但正则脱敏不能保证识别所有业务机密；不要在高敏感项目中启用通知。

飞书 Webhook 和签名密钥只保存在当前用户的 macOS Keychain。运行数据位于 `~/.codex/codex-notify/`，SQLite 与日志仅对当前用户开放。发件队列最长保留 24 小时；飞书已接收而本地未收到确认时，重试可能产生相同事件 ID 的重复消息。普通卸载保留数据，只有 `--purge` 删除运行数据。

## 开发验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src scripts tests
git diff --check
python3 -m build
```

自动测试证明源码状态机、SQLite 迁移、安装器模拟、Computer Use 链、Keychain、发件队列、重试和并发规则；它不能证明真实 Hook 信任、真实用户/子 Turn、真实飞书投递、目标 Mac 生命周期或离线重试恢复。

## 已知边界

- `agent-turn-complete` 是唯一完成来源；没有真实完成事件时不会伪造完成。
- 当前公开事件无法稳定连接子代理 `agent_id` 与子 Turn 身份，因此完整内部 Turn 抑制不在当前能力声明内。
- `SessionStart source=compact` 只记录会话信息，不建立父子边。
- 飞书若已经接收请求但本地未收到确认，重试可能产生带相同事件 ID 的重复消息。
- 当前实现依赖 macOS Keychain 和 LaunchAgent。

官方字段说明见 [Codex Hooks](https://developers.openai.com/codex/config-advanced#hooks) 和 [Codex Notifications](https://developers.openai.com/codex/config-advanced#notifications)。

## 许可证

MIT
