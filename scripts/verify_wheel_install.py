#!/usr/bin/env python3
"""Exercise an installed wheel without importing from the source checkout."""

from __future__ import annotations

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


def main() -> int:
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
