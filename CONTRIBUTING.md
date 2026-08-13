# 参与贡献

codex-notify 有意保持狭窄的支持契约：macOS、Python 3.11+、Codex Computer Use
永久拥有顶层 `notify`，并且只支持飞书自定义机器人。以下改动不在项目范围内：
取代 Computer Use 的所有权、推断私有 Turn 关系、读取 Transcript，或削弱安装器的
失败关闭检查。

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

行为变更应当同时提供回归测试。安装器变更必须覆盖首次安装、重复安装、升级、回滚、
普通卸载、彻底清理、所有权漂移，以及保留无关 Hook 和通知命令等场景。

不得提交真实凭据、Webhook、本地运行数据、个人绝对路径，或尚未确认具备再分发权利的
第三方代码。
