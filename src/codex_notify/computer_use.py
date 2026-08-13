"""Validated integration with the Computer Use Codex notification bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


COMPUTER_USE_BUNDLE_ID = "com.openai.sky.CUAService.cli"
COMPUTER_USE_TEAM_ID = "2DC432GLL2"
PREVIOUS_NOTIFY_FLAG = "--previous-notify"
COMPUTER_USE_SIGNING_REQUIREMENT = (
    f'anchor apple generic and identifier "{COMPUTER_USE_BUNDLE_ID}" '
    f'and certificate leaf[subject.OU] = "{COMPUTER_USE_TEAM_ID}"'
)
KNOWN_UNVERIFIED_BUNDLE_DIGESTS = {
    "26.804.1000633": "1caa95ca0a1a1acc2d53065b29e548ffd3a6491e4faf9d7c1caf1882ebcf0e71",
}


@dataclass(frozen=True)
class ComputerUseIntegration:
    executable: Path
    version: str
    signature_verified: bool
    notify: tuple[str, ...]
    previous_notify: tuple[str, ...] | None

    @property
    def base_notify(self) -> tuple[str, str]:
        return (str(self.executable), "turn-ended")

    def chained_notify(self, previous_notify: list[str] | tuple[str, ...]) -> list[str]:
        return [
            *self.base_notify,
            PREVIOUS_NOTIFY_FLAG,
            encode_previous_notify(previous_notify),
        ]


def encode_previous_notify(arguments: list[str] | tuple[str, ...]) -> str:
    _validate_previous_notify(arguments)
    return json.dumps(list(arguments), ensure_ascii=False, separators=(",", ":"))


def decode_previous_notify(value: str) -> tuple[str, ...]:
    try:
        arguments = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Computer Use 的 --previous-notify 不是有效 JSON") from exc
    _validate_previous_notify(arguments)
    return tuple(arguments)


def inspect_computer_use(notify: Any) -> ComputerUseIntegration:
    if not isinstance(notify, list) or not all(
        isinstance(argument, str) for argument in notify
    ):
        raise ValueError("顶层 notify 必须是 Computer Use 命令数组")
    if len(notify) not in (2, 4) or notify[1] != "turn-ended":
        raise ValueError("顶层 notify 不是受支持的 Computer Use turn-ended 命令")
    if len(notify) == 4 and notify[2] != PREVIOUS_NOTIFY_FLAG:
        raise ValueError("Computer Use notify 包含未知参数")

    executable = Path(notify[0])
    if not executable.is_absolute() or not executable.is_file() or not os.access(
        executable, os.X_OK
    ):
        raise ValueError(f"Computer Use 可执行文件缺失或不可执行：{executable}")
    try:
        contents_dir = executable.parents[1]
        client_app = executable.parents[2]
    except IndexError as exc:
        raise ValueError(f"Computer Use 可执行文件路径无效：{executable}") from exc

    info_path = contents_dir / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ValueError(f"无法读取 Computer Use Info.plist：{info_path}") from exc
    bundle_id = info.get("CFBundleIdentifier") if isinstance(info, dict) else None
    version = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
    if bundle_id != COMPUTER_USE_BUNDLE_ID:
        raise ValueError(f"Computer Use Bundle ID 不受支持：{bundle_id or '缺失'}")
    if not isinstance(version, str) or not version:
        raise ValueError("Computer Use 版本信息缺失")

    signature_verified = _verify_codesign(client_app, version)
    _verify_previous_notify_support(executable)
    previous = decode_previous_notify(notify[3]) if len(notify) == 4 else None
    return ComputerUseIntegration(
        executable=executable,
        version=version,
        signature_verified=signature_verified,
        notify=tuple(notify),
        previous_notify=previous,
    )


def _validate_previous_notify(arguments: Any) -> None:
    if not isinstance(arguments, (list, tuple)) or not arguments or not all(
        isinstance(argument, str) and argument for argument in arguments
    ):
        raise ValueError("--previous-notify 必须是非空字符串命令数组")


def _verify_codesign(client_app: Path, version: str) -> bool:
    try:
        verified = subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                f"-R={COMPUTER_USE_SIGNING_REQUIREMENT}",
                str(client_app),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        details = subprocess.run(
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(client_app)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("无法验证 Computer Use 代码签名") from exc
    output = f"{details.stdout}\n{details.stderr}"
    if details.returncode != 0:
        raise ValueError("无法读取 Computer Use 代码签名身份")
    if f"Identifier={COMPUTER_USE_BUNDLE_ID}" not in output:
        raise ValueError("Computer Use 代码签名 Identifier 不匹配")
    if f"TeamIdentifier={COMPUTER_USE_TEAM_ID}" not in output:
        raise ValueError("Computer Use 代码签名 Team ID 不匹配")
    if verified.returncode == 0:
        return True
    expected_digest = KNOWN_UNVERIFIED_BUNDLE_DIGESTS.get(version)
    actual_digest = _bundle_digest(client_app)
    if expected_digest is None or not hmac.compare_digest(
        actual_digest, expected_digest
    ):
        raise ValueError("Computer Use 验签失败且文件摘要不在兼容白名单")
    return False


def _bundle_digest(client_app: Path) -> str:
    manifest = hashlib.sha256()
    files: list[Path] = []
    for path in client_app.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Computer Use App 包含不受支持的符号链接：{path}")
        if path.is_file():
            files.append(path)
    if not files:
        raise ValueError("Computer Use App 中没有可验证文件")
    for path in sorted(files, key=lambda item: item.relative_to(client_app).as_posix()):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise ValueError(f"无法读取 Computer Use App 文件：{path}") from exc
        relative = path.relative_to(client_app).as_posix()
        manifest.update(f"{digest.hexdigest()}  {relative}\n".encode("utf-8"))
    return manifest.hexdigest()


def _verify_previous_notify_support(executable: Path) -> None:
    try:
        result = subprocess.run(
            [str(executable), "turn-ended", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("无法探测 Computer Use --previous-notify 支持状态") from exc
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or PREVIOUS_NOTIFY_FLAG not in output:
        raise ValueError("当前 Computer Use 不支持 --previous-notify")
