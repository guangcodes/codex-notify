# 参与贡献

codex-notify 有意保持狭窄的支持契约：macOS、Python 3.11+、Codex Computer Use
永久拥有顶层 `notify`，并且只支持飞书自定义机器人。以下改动不在项目范围内：
取代 Computer Use 的所有权、读取 Transcript 或 Codex 私有数据库、根据 prompt 或时间邻近
猜测 Turn 关系，或削弱安装器的失败关闭检查。当前实现只接受 Hook 原始身份与 App Server
身份、来源、时间元数据能够精确确认的关系；扩大这一边界必须同步更新隐私说明和回归测试。

引入新的 Python 依赖、外部服务、第三方源码、二进制文件、字体、图像或数据时，必须同时
说明运行时必要性，更新 `pyproject.toml`、README 的依赖表和 `THIRD_PARTY_NOTICES.md`，
并提交上游许可证要求的版权与许可文本。不要把只用于开发或 CI 的工具声明为用户运行依赖。

## 本地开发与验证

```bash
PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v
python3 -m compileall -q src scripts tests
git diff --check
python3 -m build
```

发布 CI 当前使用 Python 3.11、3.12、3.13。使用更高版本做补充验证可以发现前向兼容问题，
但不能替代这三个发布矩阵版本。

行为变更应当同时提供回归测试。安装器变更必须覆盖首次安装、重复安装、升级、回滚、
普通卸载、彻底清理、所有权漂移，以及保留无关 Hook 和通知命令等场景。

不得提交真实凭据、Webhook、本地运行数据、个人绝对路径，或尚未确认具备再分发权利的
第三方代码。

测试需要覆盖密钥形状时，应在运行时由多个不敏感片段构造确定性的假凭据，避免在源码中
保留可被 Secret scanning 识别为真实提供方凭据的连续字面量。提交前应确认 CI 的
`secrets` job 通过。

## 文档与发布同步

- 行为、安装命令、支持边界、隐私数据或外部契约发生变化时，必须同步更新 README。
- 每个版本发布前必须在 `CHANGELOG.md` 中将对应版本从“未发布”整理为带日期的版本条目，
  并与 GitHub Release 的 tag 和说明一致。
- 支持版本或漏洞报告入口变化时更新 `SECURITY.md`；依赖、vendoring、服务或商标边界变化时
  更新 `THIRD_PARTY_NOTICES.md`。
- 发布验收记录是按版本冻结的历史证据，不得用后续版本行为改写；需要新的真实验收证据时，
  新建对应版本记录并明确自动化、实机、线上产物和未验证边界。
