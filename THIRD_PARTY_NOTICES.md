# 第三方软件、服务与商标声明

本文说明 codex-notify 与第三方软件、在线服务和名称标识的边界。它是对项目
[MIT License](LICENSE) 的补充说明，不替代任何第三方自己的许可或服务条款。

## 发行包内容

codex-notify 当前没有 Python 运行时包依赖，`pyproject.toml` 中的 `dependencies` 为空。
wheel 和 sdist 不包含、复制或 vendoring 第三方运行时代码。

构建发行包需要 `setuptools>=77`。本地开发和 CI 还可能使用 `build`、`twine`、gitleaks
及 GitHub Actions。这些工具只参与构建、检查或发布，不会作为 codex-notify 运行时代码
安装到用户的私有 runtime；它们仍分别适用各自的许可证。

如果未来引入或复制第三方代码、字体、图像、二进制文件或数据，贡献者必须同时更新
依赖元数据、本文件和必要的许可证文本，不能只修改安装脚本。

## 强制外部依赖和服务

- Codex Computer Use 是强制外部依赖，永久拥有顶层 `notify`。它不属于本仓库，也不随
  codex-notify 分发；用户需要自行取得、安装和启用，并遵守其提供方条款。
- Codex 提供本项目使用的 Hook 和 `agent-turn-complete` 通知接口。Codex 客户端及其服务
  不属于本仓库。
- 飞书自定义机器人是当前唯一支持的消息目标。Webhook、签名密钥、飞书账号和消息服务
  由用户自行配置，并受飞书相关条款约束。
- GitHub 和 PyPI 仅用于源码托管及 Python 发行包分发，不属于项目运行时依赖。

## 名称与商标

本项目由社区独立维护，不是 OpenAI、Codex、飞书、GitHub 或 PyPI 的官方产品。
文档中使用相关名称仅用于说明兼容性和集成对象，不代表授权、合作、赞助或背书。
OpenAI、Codex、飞书以及其他产品名称、标识和商标归各自权利人所有。
