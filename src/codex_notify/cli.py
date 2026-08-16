"""Command-line interface for codex-notify."""

from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import plistlib
import shlex
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

from . import __version__
from .app_server_metadata import find_bundled_codex
from .computer_use import inspect_computer_use
from .constants import HOOK_STATUS_PERMISSION, HOOK_STATUS_START
from .db import NotificationStore
from .feishu import validate_webhook_url
from .hooks import hook_main
from .installer import (
    LAUNCH_AGENT_LABEL,
    LEGACY_INSTALL_STATE_VERSION,
    LEGACY_LAUNCH_AGENT_LABEL,
    INSTALL_STATE_VERSION,
    Installer,
    _hooks_explicitly_disabled,
    _launch_agent_is_missing,
    _load_install_state,
    _validate_install_state_for_integration,
    is_expected_managed_hook,
)
from .keychain import (
    FeishuCredentials,
    KeychainError,
    load_credentials,
    prompt_secret,
    store_credentials,
)
from .notifications import notification_main
from .paths import AppPaths
from .worker import run_once


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-notify")
    parser.add_argument("--version", action="version", version=f"codex-notify {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="安装或升级 Codex 通知集成")
    install.add_argument("--no-start", action="store_true", help="只写配置，不启动 LaunchAgent")
    uninstall = commands.add_parser("uninstall", help="卸载 Codex 通知集成")
    uninstall.add_argument("--purge", action="store_true", help="同时删除本地队列和日志")
    commands.add_parser("on", help="开启后续 Turn 的开始和结束通知")
    off = commands.add_parser("off", help="关闭新 Turn 通知")
    off.add_argument("--now", action="store_true", help="同时抑制正在处理和排队的通知")
    commands.add_parser("status", help="显示开关、队列和最近错误")
    commands.add_parser("test", help="立即发送一条飞书测试通知")
    commands.add_parser("configure", help="安全地将飞书凭据写入 macOS Keychain")
    commands.add_parser("doctor", help="检查凭据、Hook 和后台服务配置")
    worker = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--once", action="store_true")
    hook = commands.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument(
        "event_name",
        choices=(
            "SessionStart",
            "UserPromptSubmit",
            "SubagentStart",
            "SubagentStop",
            "PermissionRequest",
            "PreToolUse",
            "Stop",
        ),
    )
    notify = commands.add_parser("notify", help=argparse.SUPPRESS)
    notify.add_argument("payload")
    return parser


def _format_timestamp(value: str | None) -> str:
    if not value:
        return "无"
    try:
        return datetime.fromtimestamp(float(value)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "未知"


def _installed_runtime_version(package_dir: Path) -> str | None:
    init_file = package_dir / "__init__.py"
    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
    except (OSError, SyntaxError, UnicodeError):
        return None
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "__version__"
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            return statement.value.value
    return None


def _app_server_terminal_capability(binary: Path | None) -> bool:
    if binary is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="codex-notify-schema-") as directory:
            result = subprocess.run(
                [
                    str(binary),
                    "app-server",
                    "generate-json-schema",
                    "--experimental",
                    "--out",
                    directory,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if result.returncode != 0:
                return False
            root = Path(directory) / "v2"
            params = json.loads(
                (root / "ThreadTurnsListParams.json").read_text(encoding="utf-8")
            )
            response = json.loads(
                (root / "ThreadTurnsListResponse.json").read_text(encoding="utf-8")
            )
            item_views = params["definitions"]["TurnItemsView"]["oneOf"]
            statuses = response["definitions"]["TurnStatus"]["enum"]
            turn = response["definitions"]["Turn"]
            if (
                not isinstance(item_views, list)
                or not all(isinstance(item, dict) for item in item_views)
                or not isinstance(statuses, list)
                or not all(isinstance(status, str) for status in statuses)
                or not isinstance(turn, dict)
                or not isinstance(turn.get("properties"), dict)
            ):
                return False
            return (
                any(item.get("enum") == ["notLoaded"] for item in item_views)
                and set(statuses)
                == {"completed", "failed", "interrupted", "inProgress"}
                and {"id", "status", "items", "itemsView"}.issubset(
                    turn["properties"]
                )
            )
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        UnicodeError,
    ):
        return False


def _status(store: NotificationStore) -> None:
    snapshot = store.status_snapshot()
    print(f"通知：{'开启' if snapshot['enabled'] else '关闭'}")
    print(f"运行中且结束时会通知的 Turn：{snapshot['active_turns']}")
    print(f"待发送/重试：{snapshot['pending']}")
    print(f"待分类确认：{snapshot['pending_decisions']}")
    print(f"无法确认且已静默：{snapshot.get('unverified_turns', 0)}")
    print(f"关系冲突：{snapshot.get('conflict_relations', 0)}")
    print(f"等待终态校准：{snapshot.get('waiting_terminal', 0)}")
    print(
        "终态统计："
        f"completed={snapshot.get('completed_turns', 0)}，"
        f"failed={snapshot.get('failed_turns', 0)}，"
        f"interrupted={snapshot.get('interrupted_turns', 0)}"
    )
    print(
        "审批通知："
        f"总计={snapshot.get('permission_total', 0)}，"
        f"已发送={snapshot.get('permission_sent', 0)}"
    )
    terminal_query = snapshot.get("last_terminal_query_ok")
    terminal_query_label = (
        "成功" if terminal_query == "1" else "失败" if terminal_query == "0" else "无"
    )
    print(
        f"最近 App Server 终态查询：{terminal_query_label}"
        f"（{_format_timestamp(snapshot.get('last_terminal_query_at'))}）"
    )
    print(f"永久失败：{snapshot['dead']}")
    print(f"最近成功：{_format_timestamp(snapshot['last_delivery_at'])}")
    print(f"最近错误：{'有（详情已隐藏）' if snapshot['last_error'] else '无'}")


def _configure() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("Keychain 配置目前仅支持 macOS")
    webhook_url = prompt_secret("飞书机器人 Webhook URL")
    signing_secret = prompt_secret("飞书机器人签名密钥")
    validate_webhook_url(webhook_url)
    if not signing_secret:
        raise ValueError("签名密钥不能为空")
    store_credentials(FeishuCredentials(webhook_url, signing_secret))
    print("飞书凭据已安全保存到 macOS Keychain。")


def _runner_argv_ok(arguments: object, runner: Path, tail: list[str]) -> bool:
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        return False
    if len(arguments) != len(tail) + 2:
        return False
    try:
        with runner.open(encoding="utf-8") as handle:
            shebang = handle.readline().rstrip("\r\n")
    except OSError:
        return False
    if not shebang.startswith("#!"):
        return False
    expected_interpreter = shebang[2:]
    interpreter = Path(arguments[0])
    return (
        interpreter.is_file()
        and os.access(interpreter, os.X_OK)
        and arguments[0] == expected_interpreter
        and arguments[1] == str(runner)
        and arguments[2:] == tail
    )


def _hook_handler_ok(
    handler: object, runner: Path, event_name: str = "UserPromptSubmit"
) -> bool:
    if not is_expected_managed_hook(
        handler, runner=runner, event_name=event_name
    ):
        return False
    return _runner_argv_ok(
        shlex.split(handler["command"]), runner, ["hook", event_name]
    )


def _doctor(paths: AppPaths) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("macOS", platform.system() == "Darwin", platform.system()))
    bundled_codex = find_bundled_codex()
    terminal_capability = _app_server_terminal_capability(bundled_codex)
    checks.append(
        (
            "App Server 终态读取",
            terminal_capability,
            (
                "bundled Codex 可用；运行时强制 thread/turns/list "
                "itemsView=notLoaded 且 items=[]"
                if terminal_capability
                else "bundled Codex 缺失或 thread/turns/list 终态 schema 不兼容"
            ),
        )
    )
    runner_ok = paths.runner.is_file() and os.access(paths.runner, os.X_OK)
    package_dir = paths.library_dir / "codex_notify"
    library_ok = package_dir.is_dir() and (package_dir / "__init__.py").is_file()
    installed_runtime_version = (
        _installed_runtime_version(package_dir) if library_ok else None
    )
    checks.append(
        (
            "运行入口",
            runner_ok and library_ok,
            f"runner={'可执行' if runner_ok else '缺失或不可执行'}；"
            f"library={'存在' if library_ok else '缺失'}；"
            f"runtime-version={installed_runtime_version or '未知'}",
        )
    )
    try:
        credentials = load_credentials()
        validate_webhook_url(credentials.webhook_url)
        checks.append(("Keychain 凭据", bool(credentials.signing_secret), "已配置"))
    except Exception as exc:
        checks.append(("Keychain 凭据", False, str(exc)))
    config: dict[str, object] | None = None
    if paths.config_file.exists():
        try:
            config = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            pass
    hooks_enabled = not _hooks_explicitly_disabled(config or {})
    hooks_ok = False
    hook_events_ok: dict[str, bool] = {
        event_name: False
        for event_name in (
            "SessionStart",
            "UserPromptSubmit",
            "SubagentStart",
            "SubagentStop",
            "PermissionRequest",
        )
    }
    if paths.hooks_file.exists():
        try:
            document = json.loads(paths.hooks_file.read_text(encoding="utf-8"))
            hooks = document.get("hooks", {})
            for event_name in hook_events_ok:
                groups = hooks.get(event_name, []) if isinstance(hooks, dict) else []
                if not isinstance(groups, list):
                    continue
                hook_events_ok[event_name] = any(
                    (
                        event_name != "PermissionRequest"
                        or group.get("matcher") == ".*"
                    )
                    and _hook_handler_ok(handler, paths.runner, event_name)
                    for group in groups
                    if isinstance(group, dict)
                    for handler in (group.get("hooks") or [])
                    if isinstance(group.get("hooks"), list)
                )
            hooks_ok = all(hook_events_ok.values())
        except (AttributeError, OSError, json.JSONDecodeError):
            pass
    checks.append(
        (
            "Codex Hooks",
            hooks_ok and hooks_enabled,
            (
                "Hooks 已在 config.toml 中关闭"
                if not hooks_enabled
                else (
                    f"{paths.hooks_file}（SessionStart/UserPromptSubmit/SubagentStart/"
                    "SubagentStop/PermissionRequest 均需在 /hooks 确认信任）"
                )
            ),
        )
    )
    computer_use_ok = False
    install_state_ok = False
    chain_ok = False
    computer_use_detail = str(paths.config_file)
    install_state_detail = "未检查"
    integration = None
    command_chain_ok = False
    if config is not None:
        try:
            integration = inspect_computer_use(config.get("notify"))
            computer_use_ok = True
            computer_use_detail = (
                f"{integration.executable}（版本 {integration.version}，"
                "签名身份匹配，"
                f"完整性验签{'通过' if integration.signature_verified else '未通过'}，"
                "支持 --previous-notify）"
            )
            previous = integration.previous_notify
            command_chain_ok = previous is not None and _runner_argv_ok(
                list(previous), paths.runner, ["notify"]
            )
        except ValueError as exc:
            computer_use_detail = str(exc)
    if integration is not None:
        try:
            install_state = getattr(
                paths,
                "install_state",
                paths.runner.parent / "install-state.json",
            )
            state = _load_install_state(install_state, required=True)
            _validate_install_state_for_integration(state, integration)
            schema_version = state.get("schema_version")
            runtime_version = state.get("runtime_version")
            if schema_version == LEGACY_INSTALL_STATE_VERSION:
                raise ValueError("安装状态来自旧版本；请重新执行 codex-notify install")
            if schema_version != INSTALL_STATE_VERSION or runtime_version != __version__:
                raise ValueError(
                    f"runtime 版本 {runtime_version or '未知'} 与当前包版本 "
                    f"{__version__} 不一致；请重新执行 codex-notify install"
                )
            if installed_runtime_version != runtime_version:
                raise ValueError(
                    f"私有 runtime 版本 {installed_runtime_version or '未知'} 与安装状态 "
                    f"{runtime_version} 不一致；请重新执行 codex-notify install"
                )
            if install_state.stat().st_mode & 0o777 != 0o600:
                raise ValueError("codex-notify 安装状态权限必须是 0600")
            install_state_ok = True
            install_state_detail = "与当前通知链一致"
        except ValueError as exc:
            install_state_detail = str(exc)
        except OSError as exc:
            install_state_detail = f"无法检查安装状态：{exc}"
    chain_ok = command_chain_ok and install_state_ok
    checks.append(("Computer Use", computer_use_ok, computer_use_detail))
    checks.append(
        (
            "codex-notify 安装状态",
            install_state_ok,
            install_state_detail,
        )
    )
    legacy_launch_agent = getattr(paths, "legacy_launch_agent", None)
    legacy_exists = bool(
        legacy_launch_agent
        and (legacy_launch_agent.is_symlink() or legacy_launch_agent.exists())
    )
    legacy_loaded = False
    legacy_query_error: str | None = None
    if legacy_launch_agent is not None and platform.system() == "Darwin":
        result = subprocess.run(
            [
                "launchctl",
                "print",
                f"gui/{os.getuid()}/{LEGACY_LAUNCH_AGENT_LABEL}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            legacy_loaded = True
        elif not _launch_agent_is_missing(
            result, label=LEGACY_LAUNCH_AGENT_LABEL
        ):
            detail = result.stderr.strip() or result.stdout.strip() or (
                f"launchctl 退出码 {result.returncode}"
            )
            legacy_query_error = f"无法确认旧 LaunchAgent 状态：{detail}"
    legacy_residual = legacy_exists or legacy_loaded
    checks.append(
        (
            "旧 LaunchAgent 残留",
            not legacy_residual and legacy_query_error is None,
            (
                legacy_query_error
                if legacy_query_error is not None
                else (
                    (
                        f"发现旧 plist {legacy_launch_agent}"
                        if legacy_exists
                        else f"发现仍在运行的 {LEGACY_LAUNCH_AGENT_LABEL}"
                    )
                    + "；请重新执行 codex-notify install"
                    if legacy_residual
                    else f"未发现 {LEGACY_LAUNCH_AGENT_LABEL}"
                )
            ),
        )
    )
    checks.append(
        (
            "Computer Use 通知链",
            chain_ok,
            (
                "--previous-notify 正确指向 codex-notify"
                if chain_ok
                else "未连接到当前 codex-notify runner"
            ),
        )
    )
    launch_config_ok = False
    if paths.launch_agent.exists():
        try:
            with paths.launch_agent.open("rb") as handle:
                launch_config = plistlib.load(handle)
            if isinstance(launch_config, dict):
                arguments = launch_config.get("ProgramArguments")
                launch_config_ok = (
                    launch_config.get("Label") == LAUNCH_AGENT_LABEL
                    and launch_config.get("RunAtLoad") is True
                    and launch_config.get("StartInterval") == 10
                    and _runner_argv_ok(
                        arguments,
                        paths.runner,
                        ["worker", "--once"],
                    )
                )
        except (OSError, plistlib.InvalidFileException):
            pass
    launch_loaded = False
    if launch_config_ok and platform.system() == "Darwin":
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launch_loaded = result.returncode == 0
    checks.append(
        (
            "LaunchAgent",
            launch_config_ok and launch_loaded,
            f"{paths.launch_agent}（{'已加载' if launch_loaded else '未加载'}）",
        )
    )
    for name, ok, detail in checks:
        print(f"{'✓' if ok else '✗'} {name}：{detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "hook":
        if args.event_name in {"PreToolUse", "Stop"}:
            return 0
        return hook_main(args.event_name)
    if args.command == "notify":
        return notification_main(args.payload)
    if args.command in {"install", "uninstall"}:
        try:
            installer = Installer(Path(__file__).resolve().parent)
            if args.command == "install":
                installer.install(start_agent=not args.no_start)
                print(
                    "安装完成：已通过 Computer Use --previous-notify 接入完成通知。"
                    "下一步：codex-notify configure，然后在 Codex /hooks 中信任新 Hook。"
                )
            else:
                installer.uninstall(purge=args.purge)
                print("已卸载。" if args.purge else "已卸载；本地队列和日志仍保留。")
            return 0
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
    store = NotificationStore()
    try:
        if args.command == "on":
            store.set_enabled(True)
            print("通知已开启：后续已确认的用户根 Turn 会推送开始和结束。")
        elif args.command == "off":
            store.set_enabled(False, immediate=args.now)
            if args.now:
                print("通知已立即关闭：正在处理和排队的通知也已抑制。")
            else:
                print("通知已关闭：不再接收新 Turn；已通知开始的 Turn 仍会通知结束。")
        elif args.command == "status":
            _status(store)
        elif args.command == "test":
            event_key = store.enqueue_test()
            run_once(store, event_key=event_key)
            status = store.event_status(event_key)
            if status == "sent":
                print("测试通知发送成功。")
            else:
                snapshot = store.status_snapshot()
                print(f"测试通知状态：{status}；错误：{snapshot['last_error'] or '等待后台重试'}")
                return 1
        elif args.command == "configure":
            _configure()
        elif args.command == "doctor":
            return _doctor(AppPaths.default())
        elif args.command == "worker":
            run_once(store)
        return 0
    except (KeychainError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
