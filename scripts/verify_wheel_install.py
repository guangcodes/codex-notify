#!/usr/bin/env python3
"""Exercise an installed wheel without importing from the source checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import codex_notify
from codex_notify import installer as installer_module
from codex_notify.installer import Installer


def _create_computer_use(home: Path) -> Path:
    executable = (
        home
        / ".codex"
        / "computer-use"
        / "Codex Computer Use.app"
        / "Contents"
        / "SharedSupport"
        / "SkyComputerUseClient.app"
        / "Contents"
        / "MacOS"
        / "SkyComputerUseClient"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    with (executable.parents[1] / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.openai.sky.CUAService.cli",
                "CFBundleShortVersionString": "wheel-smoke",
            },
            handle,
        )
    return executable


def _normalized(value: object, home: Path) -> object:
    if isinstance(value, dict):
        return {key: _normalized(item, home) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalized(item, home) for item in value]
    if isinstance(value, str):
        for candidate in sorted({str(home), str(home.resolve())}, key=len, reverse=True):
            value = value.replace(candidate, "<HOME>")
        return value
    return value


def _runtime_manifest(root: Path) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        content = path.read_bytes()
        manifest.append(
            {
                "path": path.relative_to(root).as_posix(),
                "mode": path.stat().st_mode & 0o777,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return manifest


def _persistent_snapshot(installer: Installer, home: Path) -> dict[str, object]:
    state = json.loads(installer.paths.install_state.read_text(encoding="utf-8"))
    hooks = json.loads(installer.hooks_file.read_text(encoding="utf-8"))
    with installer.launch_agent.open("rb") as handle:
        launch_agent = plistlib.load(handle)
    snapshot = {
        "config": installer.config_file.read_text(encoding="utf-8"),
        "hooks": hooks,
        "install_state": state,
        "launch_agent": launch_agent,
        "modes": {
            "config": installer.config_file.stat().st_mode & 0o777,
            "hooks": installer.hooks_file.stat().st_mode & 0o777,
            "install_state": installer.paths.install_state.stat().st_mode & 0o777,
            "launch_agent": installer.launch_agent.stat().st_mode & 0o777,
            "runner": installer.paths.runner.stat().st_mode & 0o777,
        },
        "runner": installer.paths.runner.read_text(encoding="utf-8"),
        "runtime": _runtime_manifest(installer.paths.library_dir),
    }
    return _normalized(snapshot, home)  # type: ignore[return-value]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="将规范化后的安装持久状态写入 JSON，用于逐字段一致性比较",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    package_dir = Path(codex_notify.__file__).resolve().parent
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        executable = _create_computer_use(home)
        codex_dir = home / ".codex"
        (codex_dir / "config.toml").write_text(
            "notify = " + json.dumps([str(executable), "turn-ended"]) + "\n",
            encoding="utf-8",
        )
        (codex_dir / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
        package_cli = home / ".local" / "bin" / "codex-notify"
        package_cli.parent.mkdir(parents=True)
        package_cli.write_text("#!/bin/sh\n# package manager owned\n", encoding="utf-8")

        installer = Installer(package_dir, home=home)
        with ExitStack() as stack:
            stack.enter_context(
                patch("codex_notify.computer_use._verify_codesign", return_value=True)
            )
            stack.enter_context(
                patch(
                    "codex_notify.computer_use._verify_previous_notify_support",
                    return_value=None,
                )
            )
            stack.enter_context(
                patch.object(Installer, "_is_launch_agent_loaded", return_value=False)
            )
            stack.enter_context(
                patch.object(
                    Installer, "_is_legacy_launch_agent_loaded", return_value=False
                )
            )
            stack.enter_context(patch.object(Installer, "_bootout_launch_agent"))
            stack.enter_context(patch.object(Installer, "_reload_launch_agent"))
            stack.enter_context(patch.object(Installer, "_bootstrap_launch_agent"))

            installer.install(start_agent=True)
            installer.install(start_agent=True)

            state = json.loads(installer.paths.install_state.read_text(encoding="utf-8"))
            assert state["schema_version"] == installer_module.INSTALL_STATE_VERSION
            assert state["runtime_version"] == codex_notify.__version__
            assert package_cli.read_text(encoding="utf-8").endswith(
                "# package manager owned\n"
            )
            assert (installer.paths.library_dir / "codex_notify" / "__init__.py").is_file()
            if arguments.snapshot is not None:
                arguments.snapshot.write_text(
                    json.dumps(
                        _persistent_snapshot(installer, home),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

            installer.paths.data_dir.mkdir(exist_ok=True)
            installer.paths.log_dir.mkdir(exist_ok=True)
            (installer.paths.data_dir / "keep").write_text("data", encoding="utf-8")
            installer.uninstall(purge=False)
            assert (installer.paths.data_dir / "keep").is_file()
            assert package_cli.is_file()

            installer.install(start_agent=False)
            installer.uninstall(purge=True)
            assert not installer.paths.data_dir.exists()
            assert not installer.paths.log_dir.exists()
            assert package_cli.is_file()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
