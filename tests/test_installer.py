import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_notify import __version__, installer as installer_module
from codex_notify.computer_use import decode_previous_notify, encode_previous_notify
from codex_notify.installer import Installer


ROOT = Path(__file__).resolve().parents[1]
REAL_BOOTOUT_LAUNCH_AGENT = Installer._bootout_launch_agent
REAL_IS_LAUNCH_AGENT_LOADED = Installer._is_launch_agent_loaded
REAL_IS_LEGACY_LAUNCH_AGENT_LOADED = Installer._is_legacy_launch_agent_loaded


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
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    with (executable.parents[1] / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.openai.sky.CUAService.cli",
                "CFBundleShortVersionString": "26.804.1000633",
            },
            handle,
        )
    return executable


def _computer_use_config(executable: Path, suffix: str = "") -> str:
    return "notify = " + json.dumps([str(executable), "turn-ended"]) + "\n" + suffix


def _legacy_launch_agent_payload(installer: Installer) -> dict[str, object]:
    return installer._launch_agent_payload(
        Path(sys.executable),
        label=installer_module.LEGACY_LAUNCH_AGENT_LABEL,
    )


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.bootout_patcher = patch.object(
            Installer,
            "_bootout_launch_agent",
            autospec=True,
            return_value=None,
        )
        self.bootout = self.bootout_patcher.start()
        self.addCleanup(self.bootout_patcher.stop)
        self.loaded_patcher = patch.object(
            Installer,
            "_is_launch_agent_loaded",
            autospec=True,
            return_value=False,
        )
        self.loaded_patcher.start()
        self.addCleanup(self.loaded_patcher.stop)
        self.legacy_loaded_patcher = patch.object(
            Installer,
            "_is_legacy_launch_agent_loaded",
            autospec=True,
            return_value=False,
        )
        self.legacy_loaded_patcher.start()
        self.addCleanup(self.legacy_loaded_patcher.stop)
        self.codesign_patcher = patch(
            "codex_notify.computer_use._verify_codesign",
            return_value=True,
        )
        self.codesign_patcher.start()
        self.addCleanup(self.codesign_patcher.stop)
        self.capability_patcher = patch(
            "codex_notify.computer_use._verify_previous_notify_support",
            return_value=None,
        )
        self.capability_patcher.start()
        self.addCleanup(self.capability_patcher.stop)
        hooks_file = self.home / ".codex" / "hooks.json"
        hooks_file.parent.mkdir(parents=True)
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "/usr/bin/existing-hook"}
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        self.computer_use_executable = _create_computer_use(self.home)
        (self.home / ".codex" / "config.toml").write_text(
            _computer_use_config(self.computer_use_executable),
            encoding="utf-8",
        )
        self.installer = Installer(ROOT, home=self.home)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_uninstall_does_not_resolve_or_validate_python(self):
        with (
            patch(
                "codex_notify.installer._stable_python_executable",
                side_effect=RuntimeError("base Python missing"),
            ) as resolve_python,
            patch.object(Installer, "_bootout_launch_agent"),
        ):
            installer = Installer(ROOT, home=self.home)
            installer.uninstall()

        resolve_python.assert_not_called()

    def test_concurrent_install_and_uninstall_are_rejected_before_mutation(self):
        original_config = self.installer.config_file.read_text(encoding="utf-8")
        original_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        concurrent = Installer(ROOT, home=self.home)
        descriptor = self.installer._acquire_operation_lock()
        try:
            with self.assertRaisesRegex(RuntimeError, "安装或卸载操作正在进行"):
                concurrent.install(start_agent=False)
            with self.assertRaisesRegex(RuntimeError, "安装或卸载操作正在进行"):
                concurrent.uninstall()
        finally:
            self.installer._release_operation_lock(descriptor)

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"),
            original_config,
        )
        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"),
            original_hooks,
        )
        self.assertFalse(self.installer.paths.runner.exists())
        self.assertFalse(self.installer.paths.install_state.exists())
        self.assertEqual(
            self.installer.operation_lock_file.stat().st_mode & 0o777,
            0o600,
        )

    def test_install_validates_python_before_writing_runtime_files(self):
        with patch(
            "codex_notify.installer._stable_python_executable",
            side_effect=RuntimeError("base Python missing"),
        ):
            installer = Installer(ROOT, home=self.home)
            with self.assertRaisesRegex(RuntimeError, "base Python missing"):
                installer.install(start_agent=False)

        self.assertFalse(installer.paths.root.exists())
        self.assertFalse(installer.cli_path.exists())

    def test_snapshot_failure_does_not_create_runtime_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = _create_computer_use(home)
            (home / ".codex" / "config.toml").write_text(
                _computer_use_config(executable), encoding="utf-8"
            )
            installer = Installer(ROOT, home=home)
            with patch.object(
                installer,
                "_snapshot_install_targets",
                side_effect=RuntimeError("snapshot failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                    installer.install(start_agent=False)

            self.assertFalse(installer.paths.root.exists())

    def test_failed_install_restores_runtime_directory_layout_and_modes(self):
        root = self.installer.paths.root
        root.mkdir(parents=True)
        root.chmod(0o755)
        self.installer.paths.log_dir.mkdir()
        self.installer.paths.log_dir.chmod(0o711)
        self.assertFalse(self.installer.paths.data_dir.exists())

        with patch.object(
            self.installer,
            "_install_library",
            side_effect=RuntimeError("copy failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                self.installer.install(start_agent=False)

        self.assertEqual(root.stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            self.installer.paths.log_dir.stat().st_mode & 0o777,
            0o711,
        )
        self.assertFalse(self.installer.paths.data_dir.exists())

    def test_install_preserves_existing_hooks_and_is_idempotent(self):
        self.installer.install(start_agent=False)
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        stop_commands = [
            hook["command"]
            for group in document["hooks"]["Stop"]
            for hook in group["hooks"]
        ]
        self.assertIn("/usr/bin/existing-hook", stop_commands)
        self.assertEqual(sum("codex-notify/runner.py" in item for item in stop_commands), 0)
        for event_name in (
            "SessionStart",
            "UserPromptSubmit",
            "SubagentStart",
            "SubagentStop",
        ):
            commands = [
                handler["command"]
                for group in document["hooks"][event_name]
                for handler in group["hooks"]
            ]
            self.assertEqual(
                sum(command.endswith(f"hook {event_name}") for command in commands),
                1,
            )
        self.assertNotIn("PreToolUse", document["hooks"])
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.launch_agent.exists())
        config = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        self.assertEqual(config["notify"][0], str(self.computer_use_executable))
        self.assertEqual(config["notify"][1:3], ["turn-ended", "--previous-notify"])
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_install_succeeds_without_private_reviewer_skill(self):
        self.assertFalse((self.home / ".codex" / "skills").exists())
        self.installer.install(start_agent=False)
        self.assertFalse(self.installer.cli_path.exists())
        self.assertTrue(self.installer.paths.library_dir.exists())

    def test_reinstall_upgrades_legacy_install_state_to_current_version(self):
        self.installer.install(start_agent=False)
        state = json.loads(
            self.installer.paths.install_state.read_text(encoding="utf-8")
        )
        state["schema_version"] = installer_module.LEGACY_INSTALL_STATE_VERSION
        state.pop("runtime_version")
        self.installer.paths.install_state.write_text(
            json.dumps(state), encoding="utf-8"
        )

        self.installer.install(start_agent=False)

        upgraded = json.loads(
            self.installer.paths.install_state.read_text(encoding="utf-8")
        )
        self.assertEqual(upgraded["schema_version"], installer_module.INSTALL_STATE_VERSION)
        self.assertEqual(upgraded["runtime_version"], __version__)

    def test_install_migrates_owned_legacy_launch_agent(self):
        self.installer.install(start_agent=False)
        with self.installer.launch_agent.open("rb") as handle:
            payload = plistlib.load(handle)
        payload["Label"] = installer_module.LEGACY_LAUNCH_AGENT_LABEL
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(payload, handle)
        self.installer.launch_agent.unlink()

        with patch.object(
            self.installer, "_is_legacy_launch_agent_loaded", return_value=False
        ):
            self.installer.install(start_agent=False)

        self.assertFalse(self.installer.legacy_launch_agent.exists())
        with self.installer.launch_agent.open("rb") as handle:
            migrated = plistlib.load(handle)
        self.assertEqual(migrated["Label"], installer_module.LAUNCH_AGENT_LABEL)

    def test_install_rejects_drifted_legacy_launch_agent(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                {
                    "Label": installer_module.LEGACY_LAUNCH_AGENT_LABEL,
                    "ProgramArguments": ["/usr/bin/unrelated"],
                },
                handle,
            )

        with self.assertRaisesRegex(ValueError, "旧 LaunchAgent 不属于"):
            self.installer.install(start_agent=False)

        self.assertFalse(self.installer.paths.runner.exists())

    def test_install_rejects_legacy_launch_agent_with_drifted_fields(self):
        payload = _legacy_launch_agent_payload(self.installer)
        payload["WorkingDirectory"] = "/tmp/unrelated"
        self.installer.legacy_launch_agent.parent.mkdir(parents=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(payload, handle)

        with self.assertRaisesRegex(ValueError, "旧 LaunchAgent 不属于"):
            self.installer.install(start_agent=False)

        self.assertTrue(self.installer.legacy_launch_agent.exists())
        self.assertFalse(self.installer.paths.runner.exists())

    def test_install_rejects_unowned_current_launch_agent_before_mutation(self):
        self.installer.launch_agent.parent.mkdir(parents=True)
        replacement = plistlib.dumps(
            {
                "Label": installer_module.LAUNCH_AGENT_LABEL,
                "ProgramArguments": ["/usr/bin/unrelated"],
            }
        )
        self.installer.launch_agent.write_bytes(replacement)
        original_config = self.installer.config_file.read_bytes()
        original_hooks = self.installer.hooks_file.read_bytes()

        with self.assertRaisesRegex(ValueError, "LaunchAgent 不属于"):
            self.installer.install(start_agent=False)

        self.assertEqual(self.installer.launch_agent.read_bytes(), replacement)
        self.assertEqual(self.installer.config_file.read_bytes(), original_config)
        self.assertEqual(self.installer.hooks_file.read_bytes(), original_hooks)
        self.assertFalse(self.installer.paths.runner.exists())
        self.bootout.assert_not_called()

    def test_install_preserves_current_launch_agent_replaced_after_preflight(self):
        self.installer.install(start_agent=False)
        replacement = plistlib.dumps(
            {
                "Label": installer_module.LAUNCH_AGENT_LABEL,
                "ProgramArguments": ["/usr/bin/unrelated"],
            }
        )
        install_runner = self.installer._install_runner

        def install_runner_then_replace_launch_agent() -> None:
            install_runner()
            self.installer.launch_agent.write_bytes(replacement)

        with patch.object(
            self.installer,
            "_install_runner",
            side_effect=install_runner_then_replace_launch_agent,
        ):
            with self.assertRaisesRegex(RuntimeError, "plist.*发生变化"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.launch_agent.read_bytes(), replacement)
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_no_start_install_stops_current_agent_loaded_after_preflight(self):
        self.installer._install_launch_agent()

        with (
            patch.object(
                self.installer,
                "_is_launch_agent_loaded",
                side_effect=[False, True],
            ),
            patch.object(self.installer, "_bootout_launch_agent") as stop,
            patch.object(self.installer, "_bootstrap_launch_agent") as start,
        ):
            self.installer.install(start_agent=False)

        stop.assert_called_once_with()
        start.assert_not_called()
        self.assertTrue(self.installer.launch_agent.exists())

    def test_install_rolls_back_published_launch_agent_when_chmod_fails(self):
        path_chmod = Path.chmod

        def fail_launch_agent_chmod(path, mode, *args, **kwargs):
            if path == self.installer.launch_agent:
                raise OSError("chmod failed")
            return path_chmod(path, mode, *args, **kwargs)

        with patch.object(Path, "chmod", autospec=True, side_effect=fail_launch_agent_chmod):
            with self.assertRaisesRegex(OSError, "chmod failed"):
                self.installer.install(start_agent=False)

        self.assertFalse(self.installer.launch_agent.exists())
        self.assertFalse(self.installer.paths.install_state.exists())

    def test_legacy_launch_agent_query_does_not_depend_on_plist(self):
        query = Mock(returncode=0, stdout="loaded", stderr="")
        with patch("codex_notify.installer.subprocess.run", return_value=query) as run:
            loaded = REAL_IS_LEGACY_LAUNCH_AGENT_LOADED(self.installer)

        self.assertTrue(loaded)
        run.assert_called_once_with(
            [
                "launchctl",
                "print",
                f"gui/{os.getuid()}/{installer_module.LEGACY_LAUNCH_AGENT_LABEL}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_install_rejects_loaded_legacy_agent_without_plist(self):
        with patch.object(
            self.installer, "_is_legacy_launch_agent_loaded", return_value=True
        ):
            with self.assertRaisesRegex(ValueError, "服务仍在运行但 plist 缺失"):
                self.installer.install(start_agent=False)

        self.assertFalse(self.installer.paths.runner.exists())

    def test_install_rejects_unreadable_regular_legacy_cli(self):
        self.installer.cli_path.parent.mkdir(parents=True)
        self.installer.cli_path.write_text("legacy", encoding="utf-8")
        original_read_bytes = Path.read_bytes

        def unreadable_cli(path, *args, **kwargs):
            if path == self.installer.cli_path:
                raise OSError("permission denied")
            return original_read_bytes(path, *args, **kwargs)

        with patch.object(Path, "read_bytes", unreadable_cli):
            with self.assertRaisesRegex(ValueError, "无法确认旧 codex-notify CLI"):
                self.installer.install(start_agent=False)

        self.assertFalse(self.installer.paths.runner.exists())

    def test_uninstall_rejects_unreadable_legacy_cli_before_detaching(self):
        self.installer.install(start_agent=False)
        self.installer.cli_path.parent.mkdir(parents=True, exist_ok=True)
        self.installer.cli_path.write_text(
            f"#!{self.installer.python_executable}\n"
            "import runpy\n"
            f"runpy.run_path({str(self.installer.paths.runner)!r}, "
            'run_name="__main__")\n',
            encoding="utf-8",
        )
        original_config = self.installer.config_file.read_text(encoding="utf-8")
        original_read_bytes = Path.read_bytes

        def unreadable_cli(path, *args, **kwargs):
            if path == self.installer.cli_path:
                raise OSError("permission denied")
            return original_read_bytes(path, *args, **kwargs)

        self.bootout.reset_mock()
        with patch.object(Path, "read_bytes", unreadable_cli):
            with self.assertRaisesRegex(ValueError, "无法确认旧 codex-notify CLI"):
                self.installer.uninstall()

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), original_config
        )
        self.assertTrue(self.installer.paths.install_state.exists())
        self.bootout.assert_not_called()

    def test_install_preserves_legacy_plist_replaced_after_preflight(self):
        self.installer._install_launch_agent()
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )
        replacement = plistlib.dumps(
            {"Label": "external", "ProgramArguments": ["/usr/bin/external"]}
        )

        def replace_after_preflight():
            self.installer.legacy_launch_agent.write_bytes(replacement)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=replace_after_preflight,
            ),
            patch.object(self.installer, "_bootstrap_launch_agent"),
        ):
            with self.assertRaisesRegex(RuntimeError, "旧 LaunchAgent.*发生变化"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)
        self.assertFalse(self.installer.paths.runner.exists())

    def test_install_rollback_preserves_concurrently_created_legacy_plist(self):
        replacement = plistlib.dumps(
            {"Label": "external", "ProgramArguments": ["/usr/bin/external"]}
        )

        def create_external_plist_then_fail():
            self.installer.legacy_launch_agent.parent.mkdir(
                parents=True, exist_ok=True
            )
            self.installer.legacy_launch_agent.write_bytes(replacement)
            raise RuntimeError("runtime staging failed")

        with patch.object(
            self.installer,
            "_install_library",
            side_effect=create_external_plist_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "runtime staging failed"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)
        self.assertFalse(self.installer.paths.runner.exists())

    def test_install_stops_if_legacy_plist_appears_before_commit(self):
        replacement = plistlib.dumps(
            {"Label": "external", "ProgramArguments": ["/usr/bin/external"]}
        )
        install_launch_agent = self.installer._install_launch_agent

        def publish_then_create_external_plist(*, expected_content=None):
            install_launch_agent(expected_content=expected_content)
            self.installer.legacy_launch_agent.write_bytes(replacement)

        with patch.object(
            self.installer,
            "_install_launch_agent",
            side_effect=publish_then_create_external_plist,
        ):
            with self.assertRaisesRegex(RuntimeError, "旧 LaunchAgent.*预检后出现"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)
        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )

    def test_install_stops_if_migrated_legacy_plist_is_recreated_before_commit(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )
        replacement = plistlib.dumps(
            {"Label": "external", "ProgramArguments": ["/usr/bin/external"]}
        )
        install_launch_agent = self.installer._install_launch_agent

        def publish_then_recreate_external_plist(*, expected_content=None):
            install_launch_agent(expected_content=expected_content)
            self.installer.legacy_launch_agent.write_bytes(replacement)

        with patch.object(
            self.installer,
            "_install_launch_agent",
            side_effect=publish_then_recreate_external_plist,
        ):
            with self.assertRaisesRegex(RuntimeError, "旧 LaunchAgent.*预检后出现"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)

    def test_install_preserves_legacy_plist_replaced_during_atomic_delete(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )
        replacement = plistlib.dumps(
            {"Label": "external", "ProgramArguments": ["/usr/bin/external"]}
        )
        remove_file = installer_module._remove_file_if_unchanged

        def replace_then_remove(path, *, expected_content):
            if path == self.installer.legacy_launch_agent:
                path.write_bytes(replacement)
            return remove_file(path, expected_content=expected_content)

        with patch.object(
            installer_module,
            "_remove_file_if_unchanged",
            side_effect=replace_then_remove,
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)

    def test_uninstall_rejects_dangling_legacy_launch_agent_symlink(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        self.installer.legacy_launch_agent.symlink_to(
            self.installer.legacy_launch_agent.with_name("missing.plist")
        )

        with self.assertRaisesRegex(ValueError, "旧 LaunchAgent 不是普通文件"):
            self.installer.uninstall()

        self.assertTrue(self.installer.legacy_launch_agent.is_symlink())
        self.bootout.assert_not_called()

    def test_failed_no_start_migration_restores_loaded_legacy_launch_agent(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )

        with (
            patch.object(
                self.installer, "_is_legacy_launch_agent_loaded", return_value=True
            ),
            patch.object(self.installer, "_bootout_legacy_launch_agent"),
            patch.object(self.installer, "_bootstrap_legacy_launch_agent") as restore,
            patch.object(
                self.installer,
                "_install_launch_agent",
                side_effect=RuntimeError("new launch agent failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "new launch agent failed"):
                self.installer.install(start_agent=False)

        restore.assert_called_once_with()
        self.assertTrue(self.installer.legacy_launch_agent.exists())

    def test_install_rollback_never_overwrites_recreated_legacy_plist(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True)
        self.installer.legacy_launch_agent.write_bytes(
            plistlib.dumps(_legacy_launch_agent_payload(self.installer))
        )
        replacement = plistlib.dumps(
            {"Label": "external", "ProgramArguments": ["/usr/bin/external"]}
        )
        atomic_write = self.installer._atomic_write_bytes

        def recreate_before_atomic_restore(path, content, **kwargs):
            if path == self.installer.legacy_launch_agent and kwargs.get(
                "expected_absent"
            ):
                path.write_bytes(replacement)
            return atomic_write(path, content, **kwargs)

        with (
            patch.object(
                self.installer,
                "_is_legacy_launch_agent_loaded",
                return_value=True,
            ),
            patch.object(self.installer, "_bootout_legacy_launch_agent"),
            patch.object(
                self.installer,
                "_install_launch_agent",
                side_effect=RuntimeError("new launch agent failed"),
            ),
            patch.object(
                self.installer,
                "_atomic_write_bytes",
                side_effect=recreate_before_atomic_restore,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)

    def test_install_does_not_restart_legacy_plist_replaced_after_rollback(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True)
        original = plistlib.dumps(_legacy_launch_agent_payload(self.installer))
        self.installer.legacy_launch_agent.write_bytes(original)
        replacement_payload = _legacy_launch_agent_payload(self.installer)
        replacement_payload["ProgramArguments"][0] = "/usr/bin/python3"
        replacement = plistlib.dumps(replacement_payload)
        rollback = self.installer._rollback_install

        def rollback_then_replace(*args, **kwargs):
            errors = rollback(*args, **kwargs)
            self.installer.legacy_launch_agent.write_bytes(replacement)
            return errors

        with (
            patch.object(
                self.installer,
                "_is_legacy_launch_agent_loaded",
                return_value=True,
            ),
            patch.object(self.installer, "_bootout_legacy_launch_agent"),
            patch.object(
                self.installer,
                "_install_launch_agent",
                side_effect=RuntimeError("new launch agent failed"),
            ),
            patch.object(
                self.installer,
                "_rollback_install",
                side_effect=rollback_then_replace,
            ),
            patch.object(
                self.installer,
                "_bootstrap_legacy_launch_agent",
            ) as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        restore.assert_not_called()
        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)

    def test_install_attempts_legacy_restore_after_current_restore_fails(self):
        self.installer.install(start_agent=False)
        with self.installer.launch_agent.open("rb") as handle:
            payload = plistlib.load(handle)
        payload["Label"] = installer_module.LEGACY_LAUNCH_AGENT_LABEL
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(payload, handle)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer, "_is_legacy_launch_agent_loaded", return_value=True
            ),
            patch.object(self.installer, "_bootout_launch_agent"),
            patch.object(self.installer, "_bootout_legacy_launch_agent"),
            patch.object(
                self.installer,
                "_install_launch_agent",
                side_effect=RuntimeError("new launch agent failed"),
            ),
            patch.object(
                self.installer,
                "_bootstrap_launch_agent",
                side_effect=RuntimeError("current restore failed"),
            ),
            patch.object(
                self.installer, "_bootstrap_legacy_launch_agent"
            ) as restore_legacy,
        ):
            with self.assertRaisesRegex(RuntimeError, "current restore failed"):
                self.installer.install(start_agent=False)

        restore_legacy.assert_called_once_with()

    def test_install_does_not_restore_legacy_agent_after_filesystem_rollback_fails(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )

        with (
            patch.object(
                self.installer, "_is_legacy_launch_agent_loaded", return_value=True
            ),
            patch.object(self.installer, "_bootout_legacy_launch_agent"),
            patch.object(
                self.installer,
                "_install_launch_agent",
                side_effect=RuntimeError("new launch agent failed"),
            ),
            patch.object(
                self.installer,
                "_rollback_install",
                return_value=["runtime restore failed"],
            ),
            patch.object(
                self.installer, "_bootstrap_legacy_launch_agent"
            ) as restore_legacy,
        ):
            with self.assertRaisesRegex(RuntimeError, "runtime restore failed"):
                self.installer.install(start_agent=False)

        restore_legacy.assert_not_called()

    def test_install_upgrades_historical_managed_stop_hook(self):
        self.installer.paths.ensure_runtime_dirs()
        self.installer.paths.runner.write_text(
            f"#!{self.installer.python_executable}\n",
            encoding="utf-8",
        )
        self.installer.paths.runner.chmod(0o700)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        document["hooks"]["Stop"].append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": self.installer._hook_command("Stop"),
                        "timeout": 5,
                        "statusMessage": "Queueing Codex turn completion notification",
                    }
                ]
            }
        )
        self.installer.hooks_file.write_text(json.dumps(document), encoding="utf-8")

        self.installer.install(start_agent=False)

        upgraded = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        stop_commands = [
            handler["command"]
            for group in upgraded["hooks"]["Stop"]
            for handler in group["hooks"]
        ]
        self.assertEqual(stop_commands, ["/usr/bin/existing-hook"])

    def test_uninstall_removes_only_our_hooks(self):
        self.installer.install(start_agent=False)
        self.installer.uninstall()
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        self.assertIn("Stop", document["hooks"])
        self.assertNotIn("SessionStart", document["hooks"])
        self.assertNotIn("UserPromptSubmit", document["hooks"])
        self.assertNotIn("PreToolUse", document["hooks"])
        self.assertNotIn("SubagentStart", document["hooks"])
        self.assertNotIn("SubagentStop", document["hooks"])
        self.assertEqual(
            installer_module.tomllib.loads(
                self.installer.config_file.read_text(encoding="utf-8")
            )["notify"],
            [str(self.computer_use_executable), "turn-ended"],
        )

    def test_pretool_matcher_drift_fails_closed(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        document["hooks"]["PreToolUse"] = [
            {
                "matcher": ".*",
                "hooks": [
                    {
                        "type": "command",
                        "command": self.installer._hook_command("PreToolUse"),
                        "timeout": 5,
                    }
                ],
            }
        ]
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "PreToolUse.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "PreToolUse.*漂移"):
            self.installer.uninstall()

    def test_hook_removal_does_not_rewrite_unrelated_configuration(self):
        original = self.installer.hooks_file.read_text(encoding="utf-8")

        with patch.object(self.installer, "_write_hooks") as write_hooks:
            self.installer._remove_hooks()

        write_hooks.assert_not_called()
        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"),
            original,
        )

    def test_install_and_uninstall_fail_closed_on_managed_hook_drift(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        handler = document["hooks"]["SubagentStart"][-1]["hooks"][0]
        handler["command"] += " unexpected-extra-argument"
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), drifted)

    def test_install_and_uninstall_fail_closed_on_managed_hook_metadata_drift(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        document["hooks"]["SubagentStop"][-1]["hooks"][0]["timeout"] = 99
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "SubagentStop.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "SubagentStop.*漂移"):
            self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), drifted)

    def test_install_rechecks_hooks_immediately_before_publication(self):
        original_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        drifted_document = json.loads(original_hooks)
        drifted_document["hooks"]["SubagentStart"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            f'"{self.installer.paths.runner}" hook SubagentStart '
                            "unexpected-extra-argument"
                        ),
                    }
                ]
            }
        ]
        drifted_hooks = json.dumps(drifted_document)
        install_hooks = self.installer._install_hooks

        def drift_then_install_hooks():
            self.installer.hooks_file.write_text(drifted_hooks, encoding="utf-8")
            install_hooks()

        with patch.object(
            self.installer,
            "_install_hooks",
            side_effect=drift_then_install_hooks,
        ):
            with self.assertRaisesRegex(RuntimeError, "hooks.json.*发生变化"):
                self.installer.install(start_agent=False)

        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"), drifted_hooks
        )
        self.assertFalse(self.installer.paths.install_state.exists())
        self.assertFalse(self.installer.paths.library_dir.exists())
        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"),
            _computer_use_config(self.computer_use_executable),
        )

    def test_uninstall_rechecks_hooks_before_deleting_runtime(self):
        self.installer.install(start_agent=False)
        installed_config = self.installer.config_file.read_text(encoding="utf-8")
        drifted_document = json.loads(
            self.installer.hooks_file.read_text(encoding="utf-8")
        )
        handler = drifted_document["hooks"]["SubagentStart"][-1]["hooks"][0]
        handler["command"] += " unexpected-extra-argument"
        drifted_hooks = json.dumps(drifted_document)
        remove_hooks = self.installer._remove_hooks

        def drift_then_remove_hooks():
            self.installer.hooks_file.write_text(drifted_hooks, encoding="utf-8")
            remove_hooks()

        with patch.object(
            self.installer,
            "_remove_hooks",
            side_effect=drift_then_remove_hooks,
        ):
            with self.assertRaisesRegex(RuntimeError, "hooks.json.*发生变化"):
                self.installer.uninstall()

        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"), drifted_hooks
        )
        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), installed_config
        )
        self.assertTrue(self.installer.paths.install_state.exists())
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.library_dir.exists())

    def test_hook_write_rechecks_expected_content_after_backup(self):
        original = self.installer.hooks_file.read_bytes()
        concurrent = b'{"hooks":{"Concurrent":[]}}'
        backup = self.installer._backup

        def concurrent_hook_update(path):
            backup(path)
            path.write_bytes(concurrent)

        with patch.object(
            self.installer,
            "_backup",
            side_effect=concurrent_hook_update,
        ):
            with self.assertRaisesRegex(RuntimeError, "hooks.json.*发生变化"):
                self.installer._write_hooks(
                    {"hooks": {"UserPromptSubmit": []}},
                    expected_content=original,
                )

        self.assertEqual(self.installer.hooks_file.read_bytes(), concurrent)

    def test_hook_write_never_overwrites_a_concurrently_created_file(self):
        self.installer.hooks_file.unlink()
        concurrent = b'{"hooks":{"Concurrent":[]}}'
        link = os.link

        def concurrent_hook_create(source, destination):
            Path(destination).write_bytes(concurrent)
            return link(source, destination)

        with patch.object(os, "link", side_effect=concurrent_hook_create):
            with self.assertRaisesRegex(RuntimeError, "hooks.json.*发生变化"):
                self.installer._write_hooks({"hooks": {}})

        self.assertEqual(self.installer.hooks_file.read_bytes(), concurrent)

    def test_install_and_uninstall_fail_closed_on_managed_hook_in_unknown_event(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        document["hooks"]["FutureEvent"] = document["hooks"].pop("SubagentStart")
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "FutureEvent.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "FutureEvent.*漂移"):
            self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), drifted)

    def test_install_and_uninstall_fail_closed_on_direct_runner_hook(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        handler = document["hooks"]["SubagentStart"][-1]["hooks"][0]
        handler["command"] = (
            f'"{self.installer.paths.runner}" hook SubagentStart'
        )
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), drifted)

    def test_install_and_uninstall_fail_closed_on_runner_after_python_option(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        handler = document["hooks"]["SubagentStart"][-1]["hooks"][0]
        handler["command"] = (
            f'"{self.installer.python_executable}" -I '
            f'"{self.installer.paths.runner}" hook SubagentStart'
        )
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), drifted)

    def test_install_and_uninstall_fail_closed_on_runner_after_env_python(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        handler = document["hooks"]["SubagentStart"][-1]["hooks"][0]
        handler["command"] = (
            f'/usr/bin/env python3 "{self.installer.paths.runner}" '
            "hook SubagentStart"
        )
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "SubagentStart.*漂移"):
            self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), drifted)

    def test_install_and_uninstall_fail_closed_on_exact_runner_hook_in_unknown_event(self):
        self.installer.install(start_agent=False)
        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        handler = document["hooks"]["SubagentStart"].pop()["hooks"][0]
        handler["command"] = self.installer._hook_command("FutureEvent")
        document["hooks"]["FutureEvent"] = [{"hooks": [handler]}]
        drifted = json.dumps(document)
        self.installer.hooks_file.write_text(drifted, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "FutureEvent.*漂移"):
            self.installer.install(start_agent=False)
        with self.assertRaisesRegex(ValueError, "FutureEvent.*漂移"):
            self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), drifted)

    def test_install_and_uninstall_preserve_hooks_that_only_mention_runner(self):
        unrelated_command = f"/usr/bin/logger {self.installer.paths.runner}"
        self.installer.hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {"type": "command", "command": unrelated_command}
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        self.installer.install(start_agent=False)
        self.installer.uninstall()

        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        commands = [
            handler["command"]
            for group in document["hooks"]["UserPromptSubmit"]
            for handler in group["hooks"]
        ]
        self.assertEqual(commands, [unrelated_command])

    def test_install_and_uninstall_preserve_logger_arguments_followed_by_hook(self):
        unrelated_command = (
            f'/usr/bin/logger "{self.installer.paths.runner}" hook audit'
        )
        self.installer.hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {"type": "command", "command": unrelated_command}
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        self.installer.install(start_agent=False)
        self.installer.uninstall()

        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        commands = [
            handler["command"]
            for group in document["hooks"]["UserPromptSubmit"]
            for handler in group["hooks"]
        ]
        self.assertEqual(commands, [unrelated_command])

    def test_install_and_uninstall_preserve_non_interpreter_runner_shape(self):
        unrelated_command = (
            f'/bin/echo "{self.installer.paths.runner}" hook SubagentStart'
        )
        self.installer.hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SubagentStart": [
                            {
                                "hooks": [
                                    {"type": "command", "command": unrelated_command}
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        self.installer.install(start_agent=False)
        self.installer.uninstall()

        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        commands = [
            handler["command"]
            for group in document["hooks"]["SubagentStart"]
            for handler in group["hooks"]
        ]
        self.assertEqual(commands, [unrelated_command])

    def test_install_and_uninstall_preserve_unrelated_empty_hook_groups(self):
        empty_start_group = {"matcher": "manual", "hooks": []}
        empty_stop_group = {"matcher": "legacy", "hooks": []}
        self.installer.hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [empty_start_group],
                        "Stop": [empty_stop_group],
                    }
                }
            ),
            encoding="utf-8",
        )

        self.installer.install(start_agent=False)
        with patch.object(self.installer, "_bootout_launch_agent"):
            self.installer.uninstall()

        document = json.loads(self.installer.hooks_file.read_text(encoding="utf-8"))
        self.assertEqual(document["hooks"]["UserPromptSubmit"], [empty_start_group])
        self.assertEqual(document["hooks"]["Stop"], [empty_stop_group])

    def test_non_purge_uninstall_removes_runtime_but_preserves_data_and_logs(self):
        self.installer.install(start_agent=False)
        self.installer.paths.database.write_text("database", encoding="utf-8")
        self.installer.paths.error_log.write_text("log", encoding="utf-8")

        self.installer.uninstall(purge=False)

        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )
        self.assertEqual(self.installer.paths.runner.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.installer.paths.library_dir.exists())
        self.assertTrue(self.installer.paths.database.exists())
        self.assertTrue(self.installer.paths.error_log.exists())

    def test_purge_removes_data_but_retains_uninstalled_safety_runner(self):
        self.installer.install(start_agent=False)
        self.installer.paths.database.write_text("database", encoding="utf-8")
        self.installer.paths.error_log.write_text("log", encoding="utf-8")

        self.installer.uninstall(purge=True)

        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )
        self.assertFalse(self.installer.paths.data_dir.exists())
        self.assertFalse(self.installer.paths.log_dir.exists())
        self.assertFalse(self.installer.paths.library_dir.exists())
        self.assertFalse(self.installer.paths.install_state.exists())

    def test_repeated_uninstall_retains_uninstalled_safety_runner(self):
        self.installer.install(start_agent=False)
        self.installer.uninstall()

        self.installer.uninstall()

        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )
        self.assertEqual(self.installer.paths.runner.stat().st_mode & 0o777, 0o700)

    def test_uninstall_rejects_replaced_runner_symlink_before_mutation(self):
        self.installer.install(start_agent=False)
        original_config = self.installer.config_file.read_text(encoding="utf-8")
        original_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        unrelated = self.home / "unrelated-user-file"
        unrelated.write_text("user content\n", encoding="utf-8")
        self.installer.paths.runner.unlink()
        self.installer.paths.runner.symlink_to(unrelated)
        self.bootout.reset_mock()

        with self.assertRaisesRegex(ValueError, "runner.*符号链接"):
            self.installer.uninstall()

        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user content\n")
        self.assertTrue(self.installer.paths.runner.is_symlink())
        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), original_config
        )
        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"), original_hooks
        )
        self.bootout.assert_not_called()

    def test_purge_rejects_regular_file_runtime_root_before_uninstall_writes(self):
        self.installer.install(start_agent=False)
        original_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        original_config = self.installer.config_file.read_text(encoding="utf-8")
        shutil.rmtree(self.installer.paths.root)
        self.installer.paths.root.write_text("not a directory", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "runtime root.*不是目录"):
            self.installer.uninstall(purge=True)

        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"),
            original_hooks,
        )
        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"),
            original_config,
        )
        self.assertTrue(self.installer.launch_agent.exists())
        self.assertFalse(self.installer.cli_path.exists())
        self.assertEqual(
            self.installer.paths.root.read_text(encoding="utf-8"),
            "not a directory",
        )
        self.bootout.assert_not_called()

    def test_uninstall_stops_when_launch_agent_remains_loaded(self):
        self.installer.install(start_agent=False)
        with (
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=lambda: REAL_BOOTOUT_LAUNCH_AGENT(self.installer),
            ),
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch(
                "codex_notify.installer.subprocess.run",
                return_value=Mock(returncode=1, stderr="permission denied"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "LaunchAgent.*停止"):
                self.installer.uninstall()

        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.launch_agent.exists())

    def test_uninstall_restores_current_agent_when_legacy_stop_fails(self):
        self.installer.install(start_agent=False)
        with self.installer.launch_agent.open("rb") as handle:
            payload = plistlib.load(handle)
        payload["Label"] = installer_module.LEGACY_LAUNCH_AGENT_LABEL
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(payload, handle)
        original_config = self.installer.config_file.read_text(encoding="utf-8")

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer, "_is_legacy_launch_agent_loaded", return_value=True
            ),
            patch.object(self.installer, "_bootout_launch_agent") as stop_current,
            patch.object(
                self.installer,
                "_bootout_legacy_launch_agent",
                side_effect=RuntimeError("legacy stop failed"),
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as restore_current,
        ):
            with self.assertRaisesRegex(RuntimeError, "legacy stop failed"):
                self.installer.uninstall()

        stop_current.assert_called_once_with()
        restore_current.assert_called_once_with()
        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), original_config
        )
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_restores_loaded_agent_when_stop_verification_raises(self):
        self.installer.install(start_agent=False)

        with (
            patch.object(
                self.installer,
                "_is_launch_agent_loaded",
                side_effect=[True, RuntimeError("status still unavailable")],
            ),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=RuntimeError("status query failed after stop"),
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "status query failed"):
                self.installer.uninstall()

        restore.assert_called_once_with()
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_rejects_unowned_current_launch_agent_before_mutation(self):
        self.installer.install(start_agent=False)
        replacement = plistlib.dumps(
            {
                "Label": installer_module.LAUNCH_AGENT_LABEL,
                "ProgramArguments": ["/usr/bin/unrelated"],
            }
        )
        self.installer.launch_agent.write_bytes(replacement)
        original_config = self.installer.config_file.read_bytes()
        original_hooks = self.installer.hooks_file.read_bytes()
        self.bootout.reset_mock()

        with self.assertRaisesRegex(ValueError, "LaunchAgent 不属于"):
            self.installer.uninstall()

        self.assertEqual(self.installer.launch_agent.read_bytes(), replacement)
        self.assertEqual(self.installer.config_file.read_bytes(), original_config)
        self.assertEqual(self.installer.hooks_file.read_bytes(), original_hooks)
        self.assertTrue(self.installer.paths.install_state.exists())
        self.bootout.assert_not_called()

    def test_uninstall_does_not_restart_current_launch_agent_replaced_after_preflight(self):
        self.installer.install(start_agent=False)
        replacement = plistlib.dumps(
            {
                "Label": installer_module.LAUNCH_AGENT_LABEL,
                "ProgramArguments": ["/usr/bin/unrelated"],
            }
        )

        def replace_launch_agent_after_stop() -> None:
            self.installer.launch_agent.write_bytes(replacement)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=replace_launch_agent_after_stop,
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "卸载失败且回滚不完整"):
                self.installer.uninstall()

        restore.assert_not_called()
        self.assertEqual(self.installer.launch_agent.read_bytes(), replacement)
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_restores_hooks_when_current_launch_agent_delete_detects_drift(self):
        self.installer.install(start_agent=False)
        original_hooks = self.installer.hooks_file.read_bytes()
        original_config = self.installer.config_file.read_bytes()
        replacement = plistlib.dumps(
            {
                "Label": installer_module.LAUNCH_AGENT_LABEL,
                "ProgramArguments": ["/usr/bin/unrelated"],
            }
        )
        remove_hooks = self.installer._remove_hooks

        def remove_hooks_then_replace_launch_agent():
            written = remove_hooks()
            self.installer.launch_agent.write_bytes(replacement)
            return written

        with patch.object(
            self.installer,
            "_remove_hooks",
            side_effect=remove_hooks_then_replace_launch_agent,
        ):
            with self.assertRaisesRegex(RuntimeError, "删除期间发生并发写入"):
                self.installer.uninstall()

        self.assertEqual(self.installer.hooks_file.read_bytes(), original_hooks)
        self.assertEqual(self.installer.config_file.read_bytes(), original_config)
        self.assertEqual(self.installer.launch_agent.read_bytes(), replacement)
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_does_not_restart_replaced_legacy_launch_agent(self):
        self.installer.install(start_agent=False)
        with self.installer.launch_agent.open("rb") as handle:
            payload = plistlib.load(handle)
        payload["Label"] = installer_module.LEGACY_LAUNCH_AGENT_LABEL
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(payload, handle)
        replacement = plistlib.dumps(
            {
                "Label": "com.example.unrelated-agent",
                "ProgramArguments": ["/usr/bin/true"],
            }
        )

        def replace_legacy_plist_after_stop() -> None:
            self.installer.legacy_launch_agent.write_bytes(replacement)

        with (
            patch.object(
                self.installer,
                "_is_legacy_launch_agent_loaded",
                return_value=True,
            ),
            patch.object(
                self.installer,
                "_bootout_legacy_launch_agent",
                side_effect=replace_legacy_plist_after_stop,
            ),
            patch.object(
                self.installer,
                "_bootstrap_legacy_launch_agent",
            ) as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "卸载失败且回滚不完整"):
                self.installer.uninstall()

        restore.assert_not_called()
        self.assertEqual(self.installer.legacy_launch_agent.read_bytes(), replacement)
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_install_restores_loaded_legacy_agent_when_stop_verification_raises(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )

        with (
            patch.object(
                self.installer,
                "_is_legacy_launch_agent_loaded",
                side_effect=[True, RuntimeError("legacy status still unavailable")],
            ),
            patch.object(
                self.installer,
                "_bootout_legacy_launch_agent",
                side_effect=RuntimeError("legacy status query failed after stop"),
            ),
            patch.object(
                self.installer, "_bootstrap_legacy_launch_agent"
            ) as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "legacy status query failed"):
                self.installer.install(start_agent=False)

        restore.assert_called_once_with()
        self.assertTrue(self.installer.legacy_launch_agent.exists())

    def test_install_does_not_restore_legacy_agent_confirmed_still_loaded(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )

        with (
            patch.object(
                self.installer, "_is_legacy_launch_agent_loaded", return_value=True
            ),
            patch.object(
                self.installer,
                "_bootout_legacy_launch_agent",
                side_effect=RuntimeError("legacy service remains loaded"),
            ),
            patch.object(
                self.installer, "_bootstrap_legacy_launch_agent"
            ) as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "service remains loaded"):
                self.installer.install(start_agent=True)

        restore.assert_not_called()

    def test_install_stops_legacy_agent_loaded_after_preflight(self):
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(
                _legacy_launch_agent_payload(self.installer),
                handle,
            )

        with (
            patch.object(
                self.installer,
                "_is_legacy_launch_agent_loaded",
                side_effect=[False, True],
            ),
            patch.object(self.installer, "_bootout_legacy_launch_agent") as stop,
        ):
            self.installer.install(start_agent=False)

        stop.assert_called_once_with()
        self.assertFalse(self.installer.legacy_launch_agent.exists())

    def test_uninstall_stops_legacy_agent_loaded_after_preflight(self):
        self.installer.install(start_agent=False)
        with self.installer.launch_agent.open("rb") as handle:
            payload = plistlib.load(handle)
        payload["Label"] = installer_module.LEGACY_LAUNCH_AGENT_LABEL
        self.installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        with self.installer.legacy_launch_agent.open("wb") as handle:
            plistlib.dump(payload, handle)

        with (
            patch.object(
                self.installer,
                "_is_legacy_launch_agent_loaded",
                side_effect=[False, True],
            ),
            patch.object(self.installer, "_bootout_legacy_launch_agent") as stop,
        ):
            self.installer.uninstall()

        stop.assert_called_once_with()
        self.assertFalse(self.installer.legacy_launch_agent.exists())

    def test_uninstall_does_not_bootstrap_agent_confirmed_still_loaded(self):
        self.installer.install(start_agent=False)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=RuntimeError("stop failed and service remains loaded"),
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "service remains loaded"):
                self.installer.uninstall()

        restore.assert_not_called()
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_failed_uninstall_does_not_start_initially_unloaded_agent(self):
        self.installer.install(start_agent=False)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=False),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=RuntimeError("status query failed"),
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "status query failed"):
                self.installer.uninstall()

        restore.assert_not_called()
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_failed_uninstall_restores_agent_loaded_and_stopped_after_preflight(self):
        self.installer.install(start_agent=False)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=False),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                return_value=True,
            ),
            patch.object(
                self.installer,
                "_apply_config_uninstall",
                side_effect=RuntimeError("config restore failed"),
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "config restore failed"):
                self.installer.uninstall()

        restore.assert_called_once_with()
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_stop_checkpoint_failure_restores_agent_stopped_after_preflight(self):
        self.installer.install(start_agent=False)

        def fail_after_stop(operation, step, phase):
            if (operation, step, phase) == (
                "uninstall",
                "stop-current-service",
                "after",
            ):
                raise RuntimeError("fault after current service stop")

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=False),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                return_value=True,
            ),
            patch.object(
                self.installer,
                "_transaction_checkpoint",
                side_effect=fail_after_stop,
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as restore,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "fault after current service stop"
            ):
                self.installer.uninstall()

        restore.assert_called_once_with()
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_rejects_loaded_legacy_agent_without_plist(self):
        self.installer.install(start_agent=False)
        with patch.object(
            self.installer, "_is_legacy_launch_agent_loaded", return_value=True
        ):
            with self.assertRaisesRegex(ValueError, "服务仍在运行但 plist 缺失"):
                self.installer.uninstall()

        self.assertTrue(self.installer.paths.install_state.exists())
        self.assertTrue(self.installer.launch_agent.exists())

    def test_uninstall_rejects_invalid_hooks_before_detaching_config(self):
        self.installer.install(start_agent=False)
        installed_config = self.installer.config_file.read_text(encoding="utf-8")
        self.installer.hooks_file.write_text("not-json", encoding="utf-8")
        self.bootout.reset_mock()

        with self.assertRaisesRegex(ValueError, "hooks.json.*不是有效 JSON"):
            self.installer.uninstall()

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), installed_config
        )
        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"), "not-json"
        )
        self.assertTrue(self.installer.paths.install_state.exists())
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.launch_agent.exists())
        self.bootout.assert_not_called()

    def test_uninstall_stops_when_launch_agent_state_cannot_be_queried(self):
        self.installer.install(start_agent=False)
        bootout = Mock(returncode=1, stdout="", stderr="permission denied")
        query = Mock(returncode=1, stdout="", stderr="could not query gui domain")

        with (
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=lambda: REAL_BOOTOUT_LAUNCH_AGENT(self.installer),
            ),
            patch.object(
                self.installer,
                "_is_launch_agent_loaded",
                side_effect=lambda: REAL_IS_LAUNCH_AGENT_LOADED(self.installer),
            ),
            patch(
                "codex_notify.installer.subprocess.run",
                side_effect=[bootout, query],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "无法确认.*LaunchAgent"):
                self.installer.uninstall()

        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.launch_agent.exists())

    def test_existing_notify_fails_closed_without_changing_hooks(self):
        self.installer.config_file.write_text(
            'notify = ["/usr/bin/existing-notifier"]\n\n[model]\nname = "example"\n',
            encoding="utf-8",
        )
        original_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不是受支持的 Computer Use"):
            self.installer.install(start_agent=False)
        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), original_hooks)
        self.assertFalse(self.installer.paths.runner.exists())

    def test_install_requires_computer_use_notify(self):
        self.installer.config_file.write_text(
            '[model]\nname = "example"\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "请先安装并启用 Computer Use"):
            self.installer.install(start_agent=False)

        self.assertFalse(self.installer.paths.root.exists())
        self.bootout.assert_not_called()

    def test_install_fails_closed_when_computer_use_drops_native_chain_support(self):
        with patch(
            "codex_notify.computer_use._verify_previous_notify_support",
            side_effect=ValueError("当前 Computer Use 不支持 --previous-notify"),
        ):
            with self.assertRaisesRegex(ValueError, "不支持 --previous-notify"):
                self.installer.install(start_agent=False)

        self.assertFalse(self.installer.paths.root.exists())
        self.bootout.assert_not_called()

    def test_install_never_overwrites_an_existing_previous_notifier(self):
        occupied = [
            str(self.computer_use_executable),
            "turn-ended",
            "--previous-notify",
            encode_previous_notify(["/usr/bin/other-notifier"]),
        ]
        self.installer.config_file.write_text(
            "notify = " + json.dumps(occupied) + "\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "已被其他命令占用"):
            self.installer.install(start_agent=False)

        self.assertFalse(self.installer.paths.root.exists())
        self.bootout.assert_not_called()

    def test_install_state_is_private_and_contains_no_event_payload(self):
        self.installer.install(start_agent=False)

        state_text = self.installer.paths.install_state.read_text(encoding="utf-8")
        state = json.loads(state_text)

        self.assertEqual(self.installer.paths.install_state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state["schema_version"], installer_module.INSTALL_STATE_VERSION)
        self.assertEqual(state["runtime_version"], __version__)
        self.assertNotIn("payload", state_text.lower())
        self.assertEqual(state["computer_use"]["bundle_id"], "com.openai.sky.CUAService.cli")

    def test_reinstall_migrates_an_owned_chain_to_a_new_python_interpreter(self):
        old_python = self.home / "python-3.13"
        new_python = self.home / "python-3.14"
        old_python.write_text("", encoding="utf-8")
        new_python.write_text("", encoding="utf-8")
        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=old_python,
        ):
            Installer(ROOT, home=self.home).install(start_agent=False)

        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=new_python,
        ):
            Installer(ROOT, home=self.home).install(start_agent=False)

        notify = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertEqual(
            decode_previous_notify(notify[3]),
            (str(new_python), str(self.installer.paths.runner), "notify"),
        )
        hooks = json.loads(
            self.installer.hooks_file.read_text(encoding="utf-8")
        )["hooks"]
        for event_name in ("UserPromptSubmit", "SubagentStart", "SubagentStop"):
            managed_commands = [
                shlex.split(handler["command"])
                for group in hooks[event_name]
                for handler in group.get("hooks", [])
                if shlex.split(handler.get("command", ""))[-2:]
                == ["hook", event_name]
            ]
            self.assertEqual(
                managed_commands,
                [
                    [
                        str(new_python),
                        str(self.installer.paths.runner),
                        "hook",
                        event_name,
                    ]
                ],
            )

        Installer(ROOT, home=self.home).uninstall()
        remaining_hooks = json.loads(
            self.installer.hooks_file.read_text(encoding="utf-8")
        )["hooks"]
        for event_name in ("UserPromptSubmit", "SubagentStart", "SubagentStop"):
            self.assertNotIn(event_name, remaining_hooks)

    def test_reinstall_recovers_crash_after_config_publish_before_state_publish(self):
        old_python = self.home / "python-3.13"
        new_python = self.home / "python-3.14"
        old_python.write_text("", encoding="utf-8")
        new_python.write_text("", encoding="utf-8")
        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=old_python,
        ):
            Installer(ROOT, home=self.home).install(start_agent=False)

        interrupted = Installer(ROOT, home=self.home)
        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=new_python,
        ):
            plan = interrupted._prepare_config_install()
            with patch.object(
                interrupted,
                "_write_install_state",
                side_effect=SystemExit("simulated process death"),
            ):
                with self.assertRaisesRegex(SystemExit, "simulated process death"):
                    interrupted._install_config_notify(plan)

        self.assertTrue(interrupted.paths.pending_install_state.exists())
        old_state = json.loads(interrupted.paths.install_state.read_text(encoding="utf-8"))
        self.assertEqual(old_state["previous_notify"][0], str(old_python))
        published = installer_module.tomllib.loads(
            interrupted.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertEqual(decode_previous_notify(published[3])[0], str(new_python))

        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=new_python,
        ):
            Installer(ROOT, home=self.home).install(start_agent=False)

        recovered_state = json.loads(
            interrupted.paths.install_state.read_text(encoding="utf-8")
        )
        self.assertEqual(recovered_state["previous_notify"][0], str(new_python))
        self.assertFalse(interrupted.paths.pending_install_state.exists())

    def test_reinstall_recovers_crash_after_journal_before_config_publish(self):
        old_python = self.home / "python-3.13"
        new_python = self.home / "python-3.14"
        old_python.write_text("", encoding="utf-8")
        new_python.write_text("", encoding="utf-8")
        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=old_python,
        ):
            Installer(ROOT, home=self.home).install(start_agent=False)

        interrupted = Installer(ROOT, home=self.home)
        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=new_python,
        ):
            plan = interrupted._prepare_config_install()
            with patch.object(
                interrupted,
                "_write_config",
                side_effect=SystemExit("simulated process death"),
            ):
                with self.assertRaisesRegex(SystemExit, "simulated process death"):
                    interrupted._install_config_notify(plan)

        self.assertTrue(interrupted.paths.pending_install_state.exists())
        unchanged = installer_module.tomllib.loads(
            interrupted.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertEqual(decode_previous_notify(unchanged[3])[0], str(old_python))

        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=new_python,
        ):
            Installer(ROOT, home=self.home).install(start_agent=False)

        recovered = installer_module.tomllib.loads(
            interrupted.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertEqual(decode_previous_notify(recovered[3])[0], str(new_python))
        self.assertFalse(interrupted.paths.pending_install_state.exists())

    def test_legacy_migration_recovers_crash_after_journal_before_config_publish(self):
        legacy = [
            "/old/python3",
            str(self.installer.paths.runner),
            "notify",
        ]
        self.installer.config_file.write_text(
            f"{installer_module.CONFIG_BEGIN}\n"
            + "notify = "
            + json.dumps(legacy)
            + "\n"
            + f"{installer_module.CONFIG_END}\n",
            encoding="utf-8",
        )
        plan = self.installer._prepare_config_install()
        with patch.object(
            self.installer,
            "_write_config",
            side_effect=SystemExit("simulated process death"),
        ):
            with self.assertRaisesRegex(SystemExit, "simulated process death"):
                self.installer._install_config_notify(plan)

        pending = json.loads(
            self.installer.paths.pending_install_state.read_text(encoding="utf-8")
        )
        self.assertEqual(pending["expected_notify"], legacy)

        Installer(ROOT, home=self.home).install(start_agent=False)

        notify = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertEqual(notify[0], str(self.installer.computer_use_executable))
        self.assertEqual(notify[1:3], ["turn-ended", "--previous-notify"])
        self.assertFalse(self.installer.paths.pending_install_state.exists())

    def test_recovery_rejects_symlinked_install_state_without_touching_target(self):
        self.installer.install(start_agent=False)
        state = json.loads(
            self.installer.paths.install_state.read_text(encoding="utf-8")
        )
        pending = {
            "schema_version": installer_module.PENDING_INSTALL_STATE_VERSION,
            "expected_notify": state["original_notify"],
            "state": state,
        }
        self.installer.paths.pending_install_state.write_text(
            json.dumps(pending), encoding="utf-8"
        )
        unrelated = self.home / "unrelated-install-state-target"
        unrelated.write_text("user content\n", encoding="utf-8")
        self.installer.paths.install_state.unlink()
        self.installer.paths.install_state.symlink_to(unrelated)
        self.bootout.reset_mock()

        with self.assertRaisesRegex(ValueError, "安装目标不能是符号链接"):
            self.installer.uninstall()

        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user content\n")
        self.assertTrue(self.installer.paths.install_state.is_symlink())
        self.assertTrue(self.installer.paths.pending_install_state.exists())
        self.bootout.assert_not_called()

    def test_recovery_rejects_v2_pending_state_without_runtime_version(self):
        self.installer.install(start_agent=False)
        state = json.loads(
            self.installer.paths.install_state.read_text(encoding="utf-8")
        )
        state.pop("runtime_version")
        pending = {
            "schema_version": installer_module.PENDING_INSTALL_STATE_VERSION,
            "expected_notify": state["original_notify"],
            "state": state,
        }
        self.installer.paths.pending_install_state.write_text(
            json.dumps(pending), encoding="utf-8"
        )
        original_install_state = self.installer.paths.install_state.read_bytes()

        with self.assertRaisesRegex(ValueError, "缺少 runtime_version"):
            self.installer._recover_pending_install_state()

        self.assertEqual(
            self.installer.paths.install_state.read_bytes(), original_install_state
        )
        self.assertTrue(self.installer.paths.pending_install_state.exists())

    def test_reinstall_recovers_exact_stale_chain_from_safety_runner(self):
        self.installer.install(start_agent=False)
        installed_config = self.installer.config_file.read_text(encoding="utf-8")
        self.installer.uninstall()
        self.installer.config_file.write_text(installed_config, encoding="utf-8")
        new_python = self.home / "python-3.14"
        new_python.write_text("", encoding="utf-8")
        reinstaller = Installer(ROOT, home=self.home)

        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=new_python,
        ):
            reinstaller.install(start_agent=False)

        notify = installer_module.tomllib.loads(
            reinstaller.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertEqual(
            decode_previous_notify(notify[3]),
            (
                str(new_python),
                str(reinstaller.paths.runner),
                "notify",
            ),
        )
        self.assertTrue(reinstaller.paths.install_state.exists())
        self.assertTrue(reinstaller.paths.library_dir.exists())

    def test_repeated_uninstall_detaches_exact_stale_chain_using_safety_runner(self):
        self.installer.install(start_agent=False)
        installed_config = self.installer.config_file.read_text(encoding="utf-8")
        self.installer.uninstall()
        self.installer.config_file.write_text(installed_config, encoding="utf-8")

        self.installer.uninstall()

        notify = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertEqual(
            notify,
            [str(self.computer_use_executable), "turn-ended"],
        )
        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )

    def test_uninstall_leaves_direct_computer_use_config_after_upgrade_reset(self):
        self.installer.install(start_agent=False)
        direct = _computer_use_config(self.computer_use_executable)
        self.installer.config_file.write_text(direct, encoding="utf-8")

        self.installer.uninstall()

        self.assertEqual(self.installer.config_file.read_text(encoding="utf-8"), direct)
        self.assertFalse(self.installer.paths.install_state.exists())

    def test_uninstall_removes_our_direct_notify_after_computer_use_is_removed(self):
        self.installer.install(start_agent=False)
        previous = [
            str(self.installer.python_executable),
            str(self.installer.paths.runner),
            "notify",
        ]
        self.installer.config_file.write_text(
            "notify = " + json.dumps(previous) + "\n\n[model]\nname = \"example\"\n",
            encoding="utf-8",
        )

        self.installer.uninstall()

        parsed = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        self.assertNotIn("notify", parsed)
        self.assertEqual(parsed["model"]["name"], "example")

    def test_uninstall_fails_before_mutation_when_drift_still_references_runner(self):
        self.installer.install(start_agent=False)
        installed_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        config = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        config["notify"].append("future-argument")
        self.installer.config_file.write_text(
            "notify = " + json.dumps(config["notify"]) + "\n", encoding="utf-8"
        )
        self.bootout.reset_mock()

        with self.assertRaisesRegex(ValueError, "发生漂移"):
            self.installer.uninstall()

        self.bootout.assert_not_called()
        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"), installed_hooks
        )
        self.assertTrue(self.installer.paths.runner.exists())

    def test_uninstall_detects_runner_hidden_by_equivalent_json_unicode_escapes(self):
        self.installer.install(start_agent=False)
        config = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        config["notify"][3] = config["notify"][3].replace("/", "\\u002f")
        config["notify"].append("future-computer-use-argument")
        self.installer.config_file.write_text(
            "notify = " + json.dumps(config["notify"]) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "发生漂移"):
            self.installer.uninstall()

        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_detects_lexically_different_path_to_managed_runner(self):
        self.installer.install(start_agent=False)
        config = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        previous = list(decode_previous_notify(config["notify"][3]))
        previous[1] = (
            f"{self.installer.paths.runner.parent}/./"
            f"{self.installer.paths.runner.name}"
        )
        config["notify"][3] = encode_previous_notify(previous)
        config["notify"].append("future-computer-use-argument")
        self.installer.config_file.write_text(
            "notify = " + json.dumps(config["notify"]) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "发生漂移"):
            self.installer.uninstall()

        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_detects_runner_in_unsupported_notify_shape(self):
        self.installer.install(start_agent=False)
        equivalent_runner = (
            f"{self.installer.paths.runner.parent}/./"
            f"{self.installer.paths.runner.name}"
        )
        self.installer.config_file.write_text(
            "notify = " + json.dumps(equivalent_runner) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "发生漂移"):
            self.installer.uninstall()

        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_restores_agent_when_config_drifts_after_preflight(self):
        self.installer.install(start_agent=False)
        installed_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        changed_config = _computer_use_config(
            self.computer_use_executable, '[model]\nname = "upgraded"\n'
        )

        def computer_use_upgrade():
            self.installer.config_file.write_text(changed_config, encoding="utf-8")

        with (
            patch.object(
                self.installer,
                "_is_launch_agent_loaded",
                return_value=True,
            ),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=computer_use_upgrade,
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as bootstrap,
        ):
            with self.assertRaisesRegex(RuntimeError, "卸载期间发生变化"):
                self.installer.uninstall()

        bootstrap.assert_called_once_with()
        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"), installed_hooks
        )
        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), changed_config
        )
        self.assertTrue(self.installer.paths.runner.exists())

    def test_uninstall_rechecks_config_immediately_before_runtime_deletion(self):
        self.installer.install(start_agent=False)
        installed_config = self.installer.config_file.read_text(encoding="utf-8")
        installed_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        apply_config = self.installer._apply_config_uninstall

        def computer_use_republishes_stale_chain(plan):
            apply_config(plan)
            self.installer.config_file.write_text(
                installed_config,
                encoding="utf-8",
            )

        with patch.object(
            self.installer,
            "_apply_config_uninstall",
            side_effect=computer_use_republishes_stale_chain,
        ):
            with self.assertRaisesRegex(ValueError, "仍引用"):
                self.installer.uninstall()

        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"), installed_hooks
        )
        self.assertTrue(self.installer.launch_agent.exists())
        self.assertFalse(self.installer.cli_path.exists())
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.library_dir.exists())
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_uninstall_keeps_callable_noop_if_computer_use_republishes_after_check(self):
        self.installer.install(start_agent=False)
        installed_config = self.installer.config_file.read_text(encoding="utf-8")
        previous = decode_previous_notify(
            installer_module.tomllib.loads(installed_config)["notify"][3]
        )
        assert previous is not None
        assert_config = self.installer._assert_config_does_not_reference_runner

        def republish_after_successful_check():
            assert_config()
            self.installer.config_file.write_text(installed_config, encoding="utf-8")

        with patch.object(
            self.installer,
            "_assert_config_does_not_reference_runner",
            side_effect=republish_after_successful_check,
        ):
            self.installer.uninstall()

        self.assertIn(
            str(self.installer.paths.runner),
            self.installer.config_file.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )
        result = subprocess.run(previous, check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.installer.paths.library_dir.exists())
        self.assertFalse(self.installer.paths.install_state.exists())

    def test_install_rollback_preserves_a_concurrent_computer_use_config_update(self):
        changed_config = _computer_use_config(
            self.computer_use_executable,
            '[model]\nname = "computer-use-upgrade"\n',
        )
        install_config = self.installer._install_config_notify

        def computer_use_upgrade(plan):
            self.installer.config_file.write_text(changed_config, encoding="utf-8")
            install_config(plan)

        with patch.object(
            self.installer,
            "_install_config_notify",
            side_effect=computer_use_upgrade,
        ):
            with self.assertRaisesRegex(RuntimeError, "安装期间发生变化"):
                self.installer.install(start_agent=False)

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), changed_config
        )
        self.assertFalse(self.installer.paths.runner.exists())

    def test_install_rollback_never_deletes_a_concurrently_created_hooks_file(self):
        self.installer.hooks_file.unlink()
        concurrent = b'{"hooks":{"Concurrent":[]}}'
        replace = Path.replace

        def create_hooks_before_rollback_removal(source, destination):
            if source == self.installer.hooks_file:
                source.write_bytes(concurrent)
            return replace(source, destination)

        with (
            patch.object(
                self.installer,
                "_install_launch_agent",
                side_effect=RuntimeError("launch agent write failed"),
            ),
            patch.object(
                Path,
                "replace",
                autospec=True,
                side_effect=create_hooks_before_rollback_removal,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.hooks_file.read_bytes(), concurrent)

    def test_install_rollback_removes_its_hooks_when_file_was_initially_missing(self):
        self.installer.hooks_file.unlink()

        with patch.object(
            self.installer,
            "_install_launch_agent",
            side_effect=RuntimeError("launch agent write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "launch agent write failed"):
                self.installer.install(start_agent=False)

        self.assertFalse(self.installer.hooks_file.exists())

    def test_install_rollback_preserves_hooks_created_after_atomic_removal(self):
        self.installer.hooks_file.unlink()
        concurrent = b'{"hooks":{"Concurrent":[]}}'
        read_bytes = Path.read_bytes

        def create_hooks_after_atomic_removal(path):
            if ".hooks.json.codex-notify-rollback-" in path.name:
                self.installer.hooks_file.write_bytes(concurrent)
            return read_bytes(path)

        with (
            patch.object(
                self.installer,
                "_install_launch_agent",
                side_effect=RuntimeError("launch agent write failed"),
            ),
            patch.object(
                Path,
                "read_bytes",
                autospec=True,
                side_effect=create_hooks_after_atomic_removal,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "launch agent write failed"):
                self.installer.install(start_agent=False)

        self.assertEqual(self.installer.hooks_file.read_bytes(), concurrent)

    def test_install_rollback_preserves_a_concurrently_published_notify_chain(self):
        install_config = self.installer._install_config_notify

        def concurrent_install(plan):
            state = {
                "schema_version": installer_module.INSTALL_STATE_VERSION,
                "runtime_version": "0.1.0",
                "computer_use": {
                    "executable": str(plan.integration.executable),
                    "bundle_id": "com.openai.sky.CUAService.cli",
                    "team_id": "2DC432GLL2",
                    "version": plan.integration.version,
                    "signature_verified": plan.integration.signature_verified,
                },
                "original_notify": list(plan.original_notify),
                "original_notify_source": plan.original_notify_source,
                "installed_notify": list(plan.installed_notify),
                "previous_notify": list(plan.previous_notify),
            }
            self.installer.paths.install_state.write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            self.installer.paths.install_state.chmod(0o600)
            self.installer.config_file.write_text(
                "notify = " + json.dumps(list(plan.installed_notify)) + "\n",
                encoding="utf-8",
            )
            install_config(plan)

        with patch.object(
            self.installer,
            "_install_config_notify",
            side_effect=concurrent_install,
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        config = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        self.assertTrue(
            installer_module._notify_references_runner(
                config["notify"], self.installer.paths.runner
            )
        )
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_failed_install_keeps_runtime_when_concurrent_config_still_references_it(self):
        def fail_after_config_write():
            installed = self.installer.config_file.read_text(encoding="utf-8")
            self.installer.config_file.write_text(
                installed + '\n[model]\nname = "computer-use-upgrade"\n',
                encoding="utf-8",
            )
            raise RuntimeError("hook write failed")

        with patch.object(
            self.installer,
            "_install_hooks",
            side_effect=fail_after_config_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        notify = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )["notify"]
        self.assertIn(str(self.installer.paths.runner), notify[3])
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.library_dir.exists())
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_failed_install_decodes_json_before_checking_concurrent_runner_reference(self):
        def fail_after_config_write():
            config = installer_module.tomllib.loads(
                self.installer.config_file.read_text(encoding="utf-8")
            )
            config["notify"][3] = config["notify"][3].replace("/", "\\u002f")
            self.installer.config_file.write_text(
                "notify = "
                + json.dumps(config["notify"])
                + '\nmodel = "computer-use-upgrade"\n',
                encoding="utf-8",
            )
            raise RuntimeError("hook write failed")

        with patch.object(
            self.installer,
            "_install_hooks",
            side_effect=fail_after_config_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        config = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        self.assertEqual(
            decode_previous_notify(config["notify"][3]),
            (
                str(self.installer.python_executable),
                str(self.installer.paths.runner),
                "notify",
            ),
        )
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())

    def test_failed_install_preserves_runtime_when_concurrent_config_is_malformed(self):
        def fail_after_config_write():
            config = installer_module.tomllib.loads(
                self.installer.config_file.read_text(encoding="utf-8")
            )
            config["notify"][3] = config["notify"][3].replace("/", "\\u002f")
            self.installer.config_file.write_text(
                "notify = "
                + json.dumps(config["notify"])
                + "\ninvalid = [\n",
                encoding="utf-8",
            )
            raise RuntimeError("hook write failed")

        with patch.object(
            self.installer,
            "_install_hooks",
            side_effect=fail_after_config_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())
        self.assertIn("\\\\u002f", self.installer.config_file.read_text(encoding="utf-8"))

    def test_install_rollback_rechecks_config_before_removing_referenced_runtime(self):
        written = self.installer.config_file.read_bytes()
        backup_root = self.home / "rollback-backup"
        backup_root.mkdir()
        config_backup = backup_root / "config"
        config_backup.write_bytes(written)
        self.installer.paths.root.mkdir(parents=True)
        self.installer.paths.runner.write_text("runner", encoding="utf-8")
        self.installer.paths.install_state.write_text("{}", encoding="utf-8")
        previous = encode_previous_notify(
            [
                str(self.installer.python_executable),
                str(self.installer.paths.runner),
                "notify",
            ]
        ).replace("/", "\\u002f")
        drift = (
            "notify = "
            + json.dumps(
                [
                    str(self.computer_use_executable),
                    "turn-ended",
                    "--previous-notify",
                    previous,
                ]
            )
            + "\n"
        ).encode("utf-8")
        read_bytes = Path.read_bytes
        config_reads = 0

        def computer_use_upgrade(path):
            nonlocal config_reads
            if path == self.installer.config_file:
                config_reads += 1
                if config_reads == 2:
                    path.write_bytes(drift)
                    return drift
            return read_bytes(path)

        self.installer._transaction_after_images = {"notify": written}
        snapshots = [
            (self.installer.paths.runner, "missing", None),
            (self.installer.paths.install_state, "missing", None),
            (self.installer.config_file, "file", config_backup),
        ]
        with patch.object(Path, "read_bytes", computer_use_upgrade):
            errors = self.installer._rollback_install(backup_root, snapshots)

        self.assertTrue(any("仍引用" in error for error in errors))
        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())
        self.assertEqual(self.installer.config_file.read_bytes(), drift)

    def test_install_rollback_keeps_safety_runner_if_chain_returns_after_restore(self):
        original = self.installer.config_file.read_bytes()
        previous = encode_previous_notify(
            [
                str(self.installer.python_executable),
                str(self.installer.paths.runner),
                "notify",
            ]
        )
        installed = (
            "notify = "
            + json.dumps(
                [
                    str(self.computer_use_executable),
                    "turn-ended",
                    "--previous-notify",
                    previous,
                ]
            )
            + "\n"
        ).encode("utf-8")
        self.installer.config_file.write_bytes(installed)
        self.installer.paths.root.mkdir(parents=True)
        self.installer.paths.library_dir.mkdir()
        self.installer._install_runner()
        backup_root = self.home / "rollback-after-restore-backup"
        backup_root.mkdir()
        config_backup = backup_root / "config"
        config_backup.write_bytes(original)
        snapshots = [
            (self.installer.paths.library_dir, "missing", None),
            (self.installer.paths.runner, "missing", None),
            (self.installer.config_file, "file", config_backup),
        ]
        atomic_write = self.installer._atomic_write_bytes

        def restore_then_republish(path, content, **kwargs):
            atomic_write(path, content, **kwargs)
            if path == self.installer.config_file:
                path.write_bytes(installed)

        self.installer._transaction_after_images = {"notify": installed}
        with patch.object(
            self.installer,
            "_atomic_write_bytes",
            side_effect=restore_then_republish,
        ):
            errors = self.installer._rollback_install(backup_root, snapshots)

        self.assertEqual(errors, [])
        self.assertEqual(self.installer.config_file.read_bytes(), installed)
        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )
        self.assertFalse(self.installer.paths.library_dir.exists())

    def test_install_rollback_preserves_runtime_when_config_cannot_be_read(self):
        read_bytes = Path.read_bytes
        rollback_started = False

        def fail_after_config_write():
            nonlocal rollback_started
            rollback_started = True
            raise RuntimeError("hook write failed")

        def unreadable_config(path):
            if rollback_started and path == self.installer.config_file:
                raise OSError("config temporarily unreadable")
            return read_bytes(path)

        with (
            patch.object(
                self.installer,
                "_install_hooks",
                side_effect=fail_after_config_write,
            ),
            patch.object(Path, "read_bytes", unreadable_config),
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=False)

        self.assertTrue(self.installer.paths.runner.exists())
        self.assertTrue(self.installer.paths.install_state.exists())
        self.assertIn(
            str(self.installer.paths.runner),
            self.installer.config_file.read_text(encoding="utf-8"),
        )

    def test_config_write_rechecks_expected_content_after_backup(self):
        original = self.installer.config_file.read_text(encoding="utf-8")
        changed = original + '# changed by Computer Use\n'
        backup = self.installer._backup

        def computer_use_upgrade(path):
            backup(path)
            path.write_text(changed, encoding="utf-8")

        with patch.object(
            self.installer,
            "_backup",
            side_effect=computer_use_upgrade,
        ):
            with self.assertRaisesRegex(RuntimeError, "写入前发生变化"):
                self.installer._write_config(
                    original + '# installed by codex-notify\n',
                    expected_content=original,
                )

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), changed
        )

    def test_config_write_never_replaces_a_file_created_during_exchange(self):
        original = self.installer.config_file.read_text(encoding="utf-8")
        first_update = original + '# first concurrent Computer Use update\n'
        latest_update = original + '# latest concurrent Computer Use update\n'
        exchange_count = 0

        def computer_use_upgrade(first, second):
            nonlocal exchange_count
            self.assertTrue(self.installer.config_file.exists())
            if exchange_count == 0:
                self.installer.config_file.write_text(first_update, encoding="utf-8")
            elif exchange_count == 1:
                self.installer.config_file.write_text(latest_update, encoding="utf-8")
            exchange_count += 1
            scratch = first.with_name(first.name + ".exchange")
            first.replace(scratch)
            second.replace(first)
            scratch.replace(second)
            return True

        with patch.object(
            installer_module,
            "_exchange_paths",
            side_effect=computer_use_upgrade,
        ):
            with self.assertRaisesRegex(RuntimeError, "并发写入"):
                self.installer._write_config(
                    original + '# installed by codex-notify\n',
                    expected_content=original,
                )

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), latest_update
        )
        self.assertEqual(exchange_count, 3)

    def test_config_cleanup_failure_after_exchange_does_not_hide_publication(self):
        original = self.installer.config_file.read_text(encoding="utf-8")
        updated = original + '# installed by codex-notify\n'
        unlink = Path.unlink

        def fail_private_temporary_cleanup(path, *args, **kwargs):
            if path.name.endswith(".tmp") and path.exists():
                raise OSError("cleanup failed")
            return unlink(path, *args, **kwargs)

        self.installer._transaction_backup_files = []
        self.installer._transaction_after_images = {}
        try:
            with patch.object(Path, "unlink", fail_private_temporary_cleanup):
                self.installer._write_config(updated, expected_content=original)
        finally:
            self.installer._transaction_backup_files = None

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), updated
        )
        self.assertEqual(
            self.installer._transaction_after_images["notify"],
            updated.encode("utf-8"),
        )

    def test_config_restore_failure_records_the_already_published_write(self):
        original = self.installer.config_file.read_text(encoding="utf-8")
        concurrent = original + '# concurrent Computer Use update\n'
        updated = original + '# installed by codex-notify\n'
        exchange_count = 0

        def fail_restore(first, second):
            nonlocal exchange_count
            exchange_count += 1
            if exchange_count == 1:
                self.installer.config_file.write_text(concurrent, encoding="utf-8")
                first_content = first.read_bytes()
                second_content = second.read_bytes()
                first.write_bytes(second_content)
                second.write_bytes(first_content)
                return True
            raise OSError("restore failed")

        self.installer._transaction_backup_files = []
        self.installer._transaction_after_images = {}
        try:
            with patch.object(
                installer_module,
                "_exchange_paths",
                side_effect=fail_restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "无法恢复并发更新"):
                    self.installer._write_config(updated, expected_content=original)
        finally:
            self.installer._transaction_backup_files = None

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"), updated
        )
        self.assertEqual(
            self.installer._transaction_after_images["notify"],
            updated.encode("utf-8"),
        )

    def test_uninstall_rejects_install_state_with_mismatched_original_source(self):
        self.installer.install(start_agent=False)
        state = json.loads(self.installer.paths.install_state.read_text(encoding="utf-8"))
        state["original_notify_source"] = 'notify = ["/usr/bin/unrelated"]\n'
        self.installer.paths.install_state.write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.bootout.reset_mock()

        with self.assertRaisesRegex(ValueError, "原始 notify 配置.*不一致"):
            self.installer.uninstall()

        self.bootout.assert_not_called()
        self.assertTrue(self.installer.paths.runner.exists())

    def test_nested_multiline_array_before_notify_is_preserved(self):
        original = (
            "matrix = [\n"
            "  [1, 2],\n"
            "  [3, 4],\n"
            "]\n"
            + _computer_use_config(self.computer_use_executable)
        )
        self.installer.config_file.write_text(original, encoding="utf-8")

        self.installer.install(start_agent=False)
        self.installer.uninstall()

        self.assertEqual(self.installer.config_file.read_text(encoding="utf-8"), original)

    def test_uninstall_restores_exact_quoted_multiline_notify_assignment(self):
        original = (
            "# keep before\n"
            '"notify" = [\n'
            f"  {json.dumps(str(self.computer_use_executable))},\n"
            '  "turn-ended", # keep inline\n'
            "]\n\n"
            '[model]\nname = "example"\n'
        )
        self.installer.config_file.write_text(original, encoding="utf-8")

        self.installer.install(start_agent=False)
        self.installer.uninstall()

        self.assertEqual(self.installer.config_file.read_text(encoding="utf-8"), original)

    def test_disabled_hooks_fail_before_any_installation_write(self):
        self.installer.config_file.write_text(
            "[features]\nhooks = false\n",
            encoding="utf-8",
        )
        original_hooks = self.installer.hooks_file.read_text(encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Hooks.*关闭"):
            self.installer.install(start_agent=False)

        self.assertEqual(
            self.installer.hooks_file.read_text(encoding="utf-8"),
            original_hooks,
        )
        self.assertFalse(self.installer.paths.runner.exists())
        self.assertFalse(self.installer.cli_path.exists())

    def test_existing_package_cli_is_preserved_during_install(self):
        self.installer.cli_path.parent.mkdir(parents=True)
        self.installer.cli_path.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")

        self.installer.install(start_agent=False)

        self.assertEqual(
            self.installer.cli_path.read_text(encoding="utf-8"),
            "#!/bin/sh\necho unrelated\n",
        )
        self.assertTrue(self.installer.paths.runner.exists())

    def test_readable_non_utf8_cli_is_preserved_during_install_and_uninstall(self):
        self.installer.cli_path.parent.mkdir(parents=True)
        content = b"\xff\xfe\x00user-owned-binary"
        self.installer.cli_path.write_bytes(content)

        self.installer.install(start_agent=False)
        self.assertEqual(self.installer.cli_path.read_bytes(), content)

        self.installer.uninstall()
        self.assertEqual(self.installer.cli_path.read_bytes(), content)

    def test_existing_package_cli_symlink_is_never_followed_or_overwritten(self):
        target = self.home / "user-owned-cli"
        target.write_text("user owned\n", encoding="utf-8")
        self.installer.cli_path.parent.mkdir(parents=True)
        self.installer.cli_path.symlink_to(target)

        self.installer.install(start_agent=False)

        self.assertTrue(self.installer.cli_path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "user owned\n")
        self.assertTrue(self.installer.paths.runner.exists())

    def test_symlinked_owned_install_targets_fail_before_any_write(self):
        targets = ("runtime", "runner", "LaunchAgent")
        for label in targets:
            with self.subTest(target=label):
                with tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    installer = Installer(ROOT, home=home)
                    logical = {
                        "runtime": installer.paths.library_dir,
                        "runner": installer.paths.runner,
                        "LaunchAgent": installer.launch_agent,
                    }[label]
                    is_directory = label == "runtime"
                    logical.parent.mkdir(parents=True, exist_ok=True)
                    external = home / "external-target"
                    if is_directory:
                        external.mkdir()
                        marker = external / "marker"
                        marker.write_text("user owned\n", encoding="utf-8")
                    else:
                        external.write_text("user owned\n", encoding="utf-8")
                        marker = external
                    logical.symlink_to(external, target_is_directory=is_directory)

                    with self.assertRaisesRegex(ValueError, f"{label}.*符号链接"):
                        installer.install(start_agent=False)

                    self.assertEqual(
                        marker.read_text(encoding="utf-8"),
                        "user owned\n",
                    )
                    self.assertTrue(logical.is_symlink())

    def test_install_rejects_symlinked_runtime_root_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            external = home / "external-runtime"
            external.mkdir()
            marker = external / "user-owned"
            marker.write_text("keep\n", encoding="utf-8")
            runtime_root = home / ".codex" / "codex-notify"
            runtime_root.parent.mkdir(parents=True)
            runtime_root.symlink_to(external, target_is_directory=True)
            installer = Installer(ROOT, home=home)

            with self.assertRaisesRegex(ValueError, "runtime root.*符号链接"):
                installer.install(start_agent=False)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(list(external.iterdir()), [marker])
            self.assertTrue(runtime_root.is_symlink())

    def test_uninstall_rejects_symlinked_runtime_root_before_deleting_content(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            external = home / "external-runtime"
            library = external / "lib"
            library.mkdir(parents=True)
            marker = library / "user-owned"
            marker.write_text("keep\n", encoding="utf-8")
            runtime_root = home / ".codex" / "codex-notify"
            runtime_root.parent.mkdir(parents=True)
            runtime_root.symlink_to(external, target_is_directory=True)
            installer = Installer(ROOT, home=home)

            with self.assertRaisesRegex(ValueError, "runtime root.*符号链接"):
                installer.uninstall()

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            self.assertTrue(runtime_root.is_symlink())

    def test_install_rejects_symlinked_runtime_data_and_log_directories(self):
        for label, relative in (("runtime data", "data"), ("runtime logs", "logs")):
            with self.subTest(target=label), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                runtime_root = home / ".codex" / "codex-notify"
                runtime_root.mkdir(parents=True)
                external = home / f"external-{relative}"
                external.mkdir()
                external.chmod(0o755)
                (runtime_root / relative).symlink_to(
                    external,
                    target_is_directory=True,
                )
                installer = Installer(ROOT, home=home)

                with self.assertRaisesRegex(ValueError, f"{label}.*符号链接"):
                    installer.install(start_agent=False)

                self.assertEqual(external.stat().st_mode & 0o777, 0o755)
                self.assertEqual(list(external.iterdir()), [])

    def test_existing_legacy_managed_cli_can_be_upgraded(self):
        self.installer.cli_path.parent.mkdir(parents=True)
        self.installer.cli_path.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(str(self.installer.python_executable))} "
            f"{shlex.quote(str(self.installer.paths.runner))} \"$@\"\n",
            encoding="utf-8",
        )

        self.installer.install(start_agent=False)

        self.assertFalse(self.installer.cli_path.exists())

    def test_existing_legacy_python_cli_can_be_upgraded(self):
        self.installer.cli_path.parent.mkdir(parents=True)
        self.installer.cli_path.write_text(
            f"#!{self.installer.python_executable}\n"
            "import runpy\n"
            f"runpy.run_path({str(self.installer.paths.runner)!r}, "
            'run_name="__main__")\n',
            encoding="utf-8",
        )

        self.installer.install(start_agent=False)

        self.assertFalse(self.installer.cli_path.exists())

    def test_existing_legacy_managed_notify_is_migrated_to_computer_use(self):
        legacy = [
            "/old/python3",
            str(self.installer.paths.runner),
            "notify",
        ]
        self.installer.config_file.write_text(
            f"{installer_module.CONFIG_BEGIN}\n"
            + "notify = "
            + json.dumps(legacy)
            + "\n"
            + f"{installer_module.CONFIG_END}\n"
            + '\n[model]\nname = "example"\n',
            encoding="utf-8",
        )

        self.installer.install(start_agent=False)

        installed = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        self.assertEqual(
            installed["notify"][:3],
            [
                str(self.installer.computer_use_executable),
                "turn-ended",
                "--previous-notify",
            ],
        )
        self.assertEqual(installed["model"]["name"], "example")
        self.assertNotIn(
            installer_module.CONFIG_BEGIN,
            self.installer.config_file.read_text(encoding="utf-8"),
        )

        self.installer.uninstall()

        restored = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        self.assertEqual(
            restored["notify"],
            [str(self.installer.computer_use_executable), "turn-ended"],
        )
        self.assertEqual(restored["model"]["name"], "example")

    def test_uninstall_removes_legacy_python_cli(self):
        self.installer.cli_path.parent.mkdir(parents=True)
        self.installer.cli_path.write_text(
            f"#!{self.installer.python_executable}\n"
            "import runpy\n"
            f"runpy.run_path({str(self.installer.paths.runner)!r}, "
            'run_name="__main__")\n',
            encoding="utf-8",
        )

        self.installer.uninstall()

        self.assertFalse(self.installer.cli_path.exists())

    def test_legacy_python_cli_requires_exact_runner_and_structure(self):
        for case in ("different runner", "additional statement", "different run name"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                installer = Installer(ROOT, home=Path(directory))
                runner = (
                    installer.paths.runner
                    if case != "different runner"
                    else installer.home / "other-runner.py"
                )
                run_name = "custom" if case == "different run name" else "__main__"
                content = (
                    f"#!{installer.python_executable}\n"
                    "import runpy\n"
                    f"runpy.run_path({str(runner)!r}, run_name=\"{run_name}\")\n"
                )
                if case == "additional statement":
                    content += "print('user script')\n"
                installer.cli_path.parent.mkdir(parents=True)
                installer.cli_path.write_text(content, encoding="utf-8")

                self.assertFalse(installer._prepare_legacy_cli_migration())
                installer.uninstall()
                self.assertEqual(
                    installer.cli_path.read_text(encoding="utf-8"),
                    content,
                )

    def test_reinstall_restores_owner_permissions_on_managed_entrypoints(self):
        self.installer.install(start_agent=False)
        self.installer.paths.runner.chmod(0o400)

        self.installer.install(start_agent=False)

        self.assertEqual(
            self.installer.paths.runner.stat().st_mode & 0o700,
            0o700,
        )

    def test_multiline_toml_content_does_not_hide_existing_notify(self):
        self.installer.config_file.write_text(
            'banner = """\n[not-a-real-table]\n"""\nnotify = ["/usr/bin/existing"]\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "不是受支持的 Computer Use"):
            self.installer.install(start_agent=False)
        self.assertFalse(self.installer.paths.runner.exists())

    def test_multiline_toml_without_notify_remains_valid_after_install(self):
        self.installer.config_file.write_text(
            _computer_use_config(self.computer_use_executable)
            + 'banner = """\n[not-a-real-table]\n"""\n\n[model]\nname = "example"\n',
            encoding="utf-8",
        )
        self.installer.install(start_agent=False)
        parsed = installer_module.tomllib.loads(
            self.installer.config_file.read_text(encoding="utf-8")
        )
        self.assertEqual(parsed["model"]["name"], "example")
        self.assertEqual(parsed["notify"][1:3], ["turn-ended", "--previous-notify"])

    def test_marker_text_inside_multiline_string_is_never_removed(self):
        banner = (
            'banner = """\n'
            "# >>> codex-notify managed notification >>>\n"
            'notify = ["text only"]\n'
            "# <<< codex-notify managed notification <<<\n"
            '"""\n'
        )
        original = _computer_use_config(self.computer_use_executable) + banner
        self.installer.config_file.write_text(original, encoding="utf-8")

        self.installer.install(start_agent=False)
        self.assertIn(banner, self.installer.config_file.read_text(encoding="utf-8"))

        self.installer.uninstall()
        self.assertEqual(self.installer.config_file.read_text(encoding="utf-8"), original)

    def test_uninstall_removes_managed_block_after_user_prepends_comment(self):
        self.installer.install(start_agent=False)
        installed = self.installer.config_file.read_text(encoding="utf-8")
        self.installer.config_file.write_text(
            "# user comment\n" + installed,
            encoding="utf-8",
        )

        self.installer.uninstall()

        remaining = self.installer.config_file.read_text(encoding="utf-8")
        self.assertEqual(
            remaining,
            "# user comment\n" + _computer_use_config(self.computer_use_executable),
        )

    def test_uninstall_restores_computer_use_after_native_chain_is_reformatted(self):
        self.installer.install(start_agent=False)
        state = json.loads(self.installer.paths.install_state.read_text(encoding="utf-8"))
        arguments = state["installed_notify"]
        formatted = (
            "notify = [\n"
            + "".join(f"  {json.dumps(argument)},\n" for argument in arguments)
            + "]\n"
            "\n"
            "[model]\nname = \"example\"\n"
        )
        self.installer.config_file.write_text(formatted, encoding="utf-8")

        self.installer.uninstall()

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"),
            _computer_use_config(self.computer_use_executable)
            + '\n[model]\nname = "example"\n',
        )

    def test_uninstall_separates_restored_notify_without_trailing_newline(self):
        original = "notify = " + json.dumps(
            [str(self.computer_use_executable), "turn-ended"]
        )
        self.installer.config_file.write_text(original, encoding="utf-8")
        self.installer.install(start_agent=False)
        with self.installer.config_file.open("a", encoding="utf-8") as handle:
            handle.write('model = "example"\n')

        self.installer.uninstall()

        restored = self.installer.config_file.read_text(encoding="utf-8")
        self.assertEqual(restored, original + '\nmodel = "example"\n')
        parsed = installer_module.tomllib.loads(restored)
        self.assertEqual(
            parsed["notify"],
            [str(self.computer_use_executable), "turn-ended"],
        )
        self.assertEqual(parsed["model"], "example")

    def test_install_and_uninstall_preserve_config_leading_newlines(self):
        original = '\n\n' + _computer_use_config(
            self.computer_use_executable, '[model]\nname = "example"\n'
        )
        self.installer.config_file.write_text(original, encoding="utf-8")

        self.installer.install(start_agent=False)
        with patch.object(self.installer, "_bootout_launch_agent"):
            self.installer.uninstall()

        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"),
            original,
        )

    def test_uninstall_does_not_rewrite_unmanaged_config_with_leading_newlines(self):
        original = '\n\n[model]\nname = "example"\n'
        self.installer.config_file.write_text(original, encoding="utf-8")

        with (
            patch.object(self.installer, "_bootout_launch_agent"),
            patch.object(self.installer, "_write_config") as write_config,
        ):
            self.installer.uninstall()

        write_config.assert_not_called()
        self.assertEqual(
            self.installer.config_file.read_text(encoding="utf-8"),
            original,
        )

    def test_invalid_toml_fails_closed(self):
        self.installer.config_file.write_text("notify = [\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不是有效 TOML"):
            self.installer.install(start_agent=False)
        self.assertFalse(self.installer.paths.runner.exists())

    def test_invalid_hooks_fail_before_any_installation_write(self):
        original_config = _computer_use_config(
            self.computer_use_executable, '[model]\nname = "example"\n'
        )
        self.installer.config_file.write_text(original_config, encoding="utf-8")
        self.installer.hooks_file.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "不是有效 JSON"):
            self.installer.install(start_agent=False)

        self.assertEqual(self.installer.config_file.read_text(encoding="utf-8"), original_config)
        self.assertFalse(self.installer.paths.runner.exists())
        self.assertFalse(self.installer.cli_path.exists())
        self.assertFalse(self.installer.launch_agent.exists())

    def test_non_list_event_hooks_fail_closed_without_installation_writes(self):
        self.installer.hooks_file.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": {"unexpected": "object"}}}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "UserPromptSubmit.*array"):
            self.installer.install(start_agent=False)

        self.assertFalse(self.installer.paths.runner.exists())
        self.assertFalse(self.installer.cli_path.exists())

    def test_launch_agent_failure_rolls_back_all_installation_writes(self):
        original_hooks = self.installer.hooks_file.read_text(encoding="utf-8")
        original_config = _computer_use_config(
            self.computer_use_executable, '[model]\nname = "example"\n'
        )
        self.installer.config_file.write_text(original_config, encoding="utf-8")

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=False),
            patch.object(self.installer, "_bootout_launch_agent"),
            patch.object(
                self.installer,
                "_reload_launch_agent",
                side_effect=RuntimeError("bootstrap failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                self.installer.install(start_agent=True)

        self.assertEqual(self.installer.hooks_file.read_text(encoding="utf-8"), original_hooks)
        self.assertEqual(self.installer.config_file.read_text(encoding="utf-8"), original_config)
        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            installer_module.UNINSTALLED_RUNNER_CONTENT,
        )
        self.assertFalse(self.installer.paths.library_dir.exists())
        self.assertFalse(self.installer.cli_path.exists())
        self.assertFalse(self.installer.launch_agent.exists())

    def test_reinstall_stops_loaded_agent_before_replacing_runtime(self):
        self.installer.install(start_agent=False)
        events = []
        original_install_library = self.installer._install_library

        def install_library():
            events.append("replace-runtime")
            original_install_library()

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=lambda: events.append("stop-agent"),
            ),
            patch.object(
                self.installer,
                "_bootstrap_launch_agent",
                side_effect=lambda: events.append("start-agent"),
            ),
            patch.object(
                self.installer,
                "_install_library",
                side_effect=install_library,
            ),
        ):
            self.installer.install(start_agent=True)

        self.assertEqual(events[0], "stop-agent")
        self.assertLess(events.index("stop-agent"), events.index("replace-runtime"))
        self.assertEqual(events[-1], "start-agent")

    def test_no_start_reinstall_stops_loaded_agent_and_leaves_it_stopped(self):
        self.installer.install(start_agent=False)
        events = []
        original_install_library = self.installer._install_library

        def install_library():
            events.append("replace-runtime")
            original_install_library()

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=lambda: events.append("stop-agent"),
            ),
            patch.object(self.installer, "_install_library", side_effect=install_library),
            patch.object(self.installer, "_bootstrap_launch_agent") as bootstrap,
            patch.object(self.installer, "_reload_launch_agent") as reload_agent,
        ):
            self.installer.install(start_agent=False)

        self.assertEqual(events[0], "stop-agent")
        self.assertLess(events.index("stop-agent"), events.index("replace-runtime"))
        bootstrap.assert_not_called()
        reload_agent.assert_not_called()

    def test_failed_no_start_reinstall_restores_loaded_agent(self):
        self.installer.install(start_agent=False)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(self.installer, "_bootout_launch_agent"),
            patch.object(
                self.installer,
                "_install_library",
                side_effect=RuntimeError("copy failed"),
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as bootstrap,
        ):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                self.installer.install(start_agent=False)

        bootstrap.assert_called_once_with()

    def test_stop_failure_before_writes_does_not_restore_snapshots(self):
        self.installer.install(start_agent=False)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(
                self.installer,
                "_bootout_launch_agent",
                side_effect=[RuntimeError("stop failed"), None],
            ),
            patch.object(
                self.installer,
                "_rollback_install",
                return_value=[],
            ) as rollback_install,
            patch.object(self.installer, "_install_library") as install_library,
            patch.object(self.installer, "_bootstrap_launch_agent"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                self.installer.install(start_agent=True)

        rollback_install.assert_not_called()
        install_library.assert_not_called()

    def test_incomplete_rollback_does_not_restart_loaded_agent(self):
        self.installer.install(start_agent=False)

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(self.installer, "_bootout_launch_agent"),
            patch.object(
                self.installer,
                "_install_library",
                side_effect=RuntimeError("copy failed"),
            ),
            patch.object(
                self.installer,
                "_rollback_install",
                return_value=["restore failed"],
            ),
            patch.object(
                self.installer,
                "_restore_runtime_directories",
                return_value=[],
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as bootstrap,
        ):
            with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                self.installer.install(start_agent=True)

        bootstrap.assert_not_called()

    def test_keyboard_interrupt_rolls_back_and_restores_loaded_agent(self):
        self.installer.install(start_agent=False)
        original_runner = self.installer.paths.runner.read_text(encoding="utf-8")

        with (
            patch.object(self.installer, "_is_launch_agent_loaded", return_value=True),
            patch.object(self.installer, "_bootout_launch_agent"),
            patch.object(
                self.installer,
                "_install_library",
                side_effect=KeyboardInterrupt(),
            ),
            patch.object(self.installer, "_bootstrap_launch_agent") as bootstrap,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.installer.install(start_agent=True)

        bootstrap.assert_called_once_with()
        self.assertEqual(
            self.installer.paths.runner.read_text(encoding="utf-8"),
            original_runner,
        )

    def test_install_uses_stable_base_interpreter_for_every_entry_point(self):
        stable_python = Path("/opt/stable-python/bin/python3")
        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=stable_python,
        ):
            installer = Installer(ROOT, home=self.home)
            installer.install(start_agent=False)

        self.assertTrue(
            installer.paths.runner.read_text(encoding="utf-8").startswith(f"#!{stable_python}")
        )
        hooks = json.loads(installer.hooks_file.read_text(encoding="utf-8"))
        start_command = hooks["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"]
        self.assertIn(str(stable_python), start_command)
        config = installer.config_file.read_text(encoding="utf-8")
        self.assertIn(str(stable_python), config)
        with installer.launch_agent.open("rb") as handle:
            launch_agent = plistlib.load(handle)
        self.assertEqual(launch_agent["ProgramArguments"][0], str(stable_python))

    def test_generated_runner_pins_installed_runtime_root(self):
        self.installer.install(start_agent=False)
        override = self.home / "different-runtime"
        environment = os.environ.copy()
        environment["CODEX_NOTIFY_HOME"] = str(override)

        result = subprocess.run(
            [
                str(self.installer.python_executable),
                str(self.installer.paths.runner),
                "status",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.installer.paths.database.exists())
        self.assertFalse(override.exists())

    def test_stable_interpreter_uses_conda_base_python(self):
        conda_root = self.home / "conda"
        environment = conda_root / "envs" / "temporary"
        environment.mkdir(parents=True)
        environment_python = environment / "bin" / "python3"
        base_python = conda_root / "bin" / "python3"
        base_python.parent.mkdir(parents=True)
        base_python.write_text("", encoding="utf-8")

        with (
            patch("codex_notify.installer.sys.prefix", str(environment)),
            patch("codex_notify.installer.sys.base_prefix", str(environment)),
            patch("codex_notify.installer.sys.executable", str(environment_python)),
            patch.dict(
                "codex_notify.installer.os.environ",
                {
                    "CONDA_PREFIX": str(environment),
                    "CONDA_PYTHON_EXE": str(base_python),
                },
                clear=False,
            ),
            patch(
                "codex_notify.installer.subprocess.run",
                return_value=Mock(returncode=0, stdout="3.11\n", stderr=""),
            ),
        ):
            selected = installer_module._stable_python_executable()

        self.assertEqual(selected, base_python)

    def test_stable_interpreter_rejects_incompatible_conda_base_python(self):
        conda_root = self.home / "conda"
        environment = conda_root / "envs" / "temporary"
        environment.mkdir(parents=True)
        base_python = conda_root / "bin" / "python3"
        base_python.parent.mkdir(parents=True)
        base_python.write_text("", encoding="utf-8")

        with (
            patch("codex_notify.installer.sys.prefix", str(environment)),
            patch("codex_notify.installer.sys.base_prefix", str(environment)),
            patch("codex_notify.installer.sys.executable", str(environment / "bin/python3")),
            patch.dict(
                "codex_notify.installer.os.environ",
                {
                    "CONDA_PREFIX": str(environment),
                    "CONDA_PYTHON_EXE": str(base_python),
                },
                clear=False,
            ),
            patch(
                "codex_notify.installer.subprocess.run",
                return_value=Mock(returncode=0, stdout="3.10\n", stderr=""),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Python 3.11"):
                installer_module._stable_python_executable()

    def test_legacy_cli_detection_handles_interpreter_paths_with_spaces(self):
        interpreter = self.home / "Conda Environments" / "base" / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("", encoding="utf-8")
        with patch(
            "codex_notify.installer._stable_python_executable",
            return_value=interpreter,
        ):
            installer = Installer(ROOT, home=self.home)
            installer.cli_path.parent.mkdir(parents=True)
            installer.cli_path.write_text(
                "#!/bin/sh\n"
                f"{installer_module.CLI_MARKER}\n"
                f"exec {shlex.quote(str(interpreter))} "
                f"{shlex.quote(str(installer.paths.runner))} \"$@\"\n",
                encoding="utf-8",
            )

        self.assertTrue(installer._prepare_legacy_cli_migration())

    def test_uninstall_preserves_cli_replaced_after_installation(self):
        self.installer.install(start_agent=False)
        self.installer.cli_path.parent.mkdir(parents=True)
        self.installer.cli_path.write_text(
            "#!/bin/sh\necho user replacement\n",
            encoding="utf-8",
        )

        self.installer.uninstall()

        self.assertEqual(
            self.installer.cli_path.read_text(encoding="utf-8"),
            "#!/bin/sh\necho user replacement\n",
        )

    def test_runner_is_installed_with_atomic_write(self):
        self.installer.paths.ensure_runtime_dirs()
        with patch.object(
            self.installer,
            "_atomic_write_text",
            wraps=self.installer._atomic_write_text,
        ) as atomic_write:
            self.installer._install_runner()

        targets = [invocation.args[0] for invocation in atomic_write.call_args_list]
        self.assertEqual(targets, [self.installer.paths.runner])

    def test_launch_agent_write_failure_preserves_existing_plist(self):
        self.installer.launch_agent.parent.mkdir(parents=True)
        original = b"existing launch agent\n"
        self.installer.launch_agent.write_bytes(original)

        with patch(
            "codex_notify.installer.os.fsync",
            side_effect=OSError("fsync failed"),
        ):
            with self.assertRaisesRegex(OSError, "fsync failed"):
                self.installer._install_launch_agent()

        self.assertEqual(self.installer.launch_agent.read_bytes(), original)
        self.assertEqual(
            list(
                self.installer.launch_agent.parent.glob(
                    f".{self.installer.launch_agent.name}.codex-notify-*.tmp"
                )
            ),
            [],
        )

    def test_config_symlink_survives_install_and_uninstall(self):
        dotfiles = self.home / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "config.toml"
        target.write_text(
            _computer_use_config(
                self.computer_use_executable, '[model]\nname = "example"\n'
            ),
            encoding="utf-8",
        )
        self.installer.config_file.unlink()
        self.installer.config_file.symlink_to(target)

        self.installer.install(start_agent=False)
        self.assertTrue(self.installer.config_file.is_symlink())
        self.assertIn("--previous-notify", target.read_text(encoding="utf-8"))

        self.installer.uninstall()
        self.assertTrue(self.installer.config_file.is_symlink())
        self.assertNotIn("--previous-notify", target.read_text(encoding="utf-8"))

    def test_hooks_symlink_survives_install(self):
        dotfiles = self.home / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "hooks.json"
        target.write_text(self.installer.hooks_file.read_text(encoding="utf-8"), encoding="utf-8")
        self.installer.hooks_file.unlink()
        self.installer.hooks_file.symlink_to(target)

        self.installer.install(start_agent=False)
        self.assertTrue(self.installer.hooks_file.is_symlink())
        self.assertIn("codex-notify/runner.py", target.read_text(encoding="utf-8"))

    def test_atomic_write_creates_replacement_with_restricted_mode(self):
        target = self.home / "sensitive.json"
        observed_pre_chmod_modes = []
        original_chmod = Path.chmod

        def inspect_chmod(path, mode, *args, **kwargs):
            if path.name.endswith(".tmp"):
                observed_pre_chmod_modes.append(path.stat().st_mode & 0o777)
            return original_chmod(path, mode, *args, **kwargs)

        previous_umask = os.umask(0o022)
        try:
            with patch.object(Path, "chmod", new=inspect_chmod):
                self.installer._atomic_write_text(target, "secret", default_mode=0o600)
        finally:
            os.umask(previous_umask)

        self.assertNotIn(0o644, observed_pre_chmod_modes)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_atomic_write_reapplies_requested_mode_after_umask(self):
        target = self.home / "runner.py"
        previous_umask = os.umask(0o777)
        try:
            self.installer._atomic_write_text(
                target,
                "#!/usr/bin/python3\n",
                default_mode=0o700,
            )
        finally:
            os.umask(previous_umask)

        self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_atomic_write_removes_group_and_world_write_permissions(self):
        for default_mode, existing_mode in ((0o700, 0o777), (0o600, 0o666)):
            with self.subTest(default_mode=oct(default_mode)):
                target = self.home / f"managed-{default_mode:o}"
                target.write_text("old\n", encoding="utf-8")
                target.chmod(existing_mode)

                self.installer._atomic_write_text(
                    target,
                    "new\n",
                    default_mode=default_mode,
                )

                mode = target.stat().st_mode & 0o777
                self.assertEqual(mode & 0o022, 0)
                self.assertEqual(mode & default_mode, default_mode)

    def test_backup_of_sensitive_file_is_restricted(self):
        target = self.home / "config.toml"
        target.write_text("secret", encoding="utf-8")
        target.chmod(0o644)

        self.installer._backup(target)

        backups = list(self.home.glob("config.toml.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_library_is_fully_staged_before_existing_runtime_is_replaced(self):
        self.installer._install_library()
        marker = self.installer.paths.library_dir / "codex_notify" / "old-runtime"
        marker.write_text("old", encoding="utf-8")
        original_copytree = installer_module.shutil.copytree
        observed_existing_runtime = []

        def inspect_copytree(source, destination, *args, **kwargs):
            if Path(source) == ROOT / "src" / "codex_notify":
                observed_existing_runtime.append(marker.exists())
            return original_copytree(source, destination, *args, **kwargs)

        with patch("codex_notify.installer.shutil.copytree", side_effect=inspect_copytree):
            self.installer._install_library()

        self.assertEqual(observed_existing_runtime, [True])
        self.assertFalse(marker.exists())
        self.assertEqual(list(self.installer.paths.root.glob(".lib-*-*")), [])

    def test_reinstall_succeeds_when_directory_swap_uses_fallback(self):
        self.installer._install_library()
        marker = self.installer.paths.library_dir / "codex_notify" / "old-runtime"
        marker.write_text("old", encoding="utf-8")

        with patch("codex_notify.installer.sys.platform", "linux"):
            self.installer._install_library()

        self.assertFalse(marker.exists())
        self.assertTrue(
            (self.installer.paths.library_dir / "codex_notify" / "installer.py").exists()
        )
        self.assertEqual(list(self.installer.paths.root.glob(".lib-*-*")), [])

class LifecycleTransactionMatrixTests(unittest.TestCase):
    def setUp(self):
        self.codesign_patcher = patch(
            "codex_notify.computer_use._verify_codesign",
            return_value=True,
        )
        self.codesign_patcher.start()
        self.addCleanup(self.codesign_patcher.stop)
        self.capability_patcher = patch(
            "codex_notify.computer_use._verify_previous_notify_support",
            return_value=None,
        )
        self.capability_patcher.start()
        self.addCleanup(self.capability_patcher.stop)
        self.loaded_patcher = patch.object(
            Installer,
            "_is_launch_agent_loaded",
            autospec=True,
            return_value=False,
        )
        self.loaded_patcher.start()
        self.addCleanup(self.loaded_patcher.stop)
        self.legacy_loaded_patcher = patch.object(
            Installer,
            "_is_legacy_launch_agent_loaded",
            autospec=True,
            return_value=False,
        )
        self.legacy_loaded_patcher.start()
        self.addCleanup(self.legacy_loaded_patcher.stop)
        self.bootout_patcher = patch.object(
            Installer,
            "_bootout_launch_agent",
            autospec=True,
            return_value=None,
        )
        self.bootout_patcher.start()
        self.addCleanup(self.bootout_patcher.stop)

    def _fixture(self, home: Path) -> Installer:
        executable = _create_computer_use(home)
        dot_codex = home / ".codex"
        dot_codex.mkdir(parents=True, exist_ok=True)
        (dot_codex / "config.toml").write_text(
            _computer_use_config(executable),
            encoding="utf-8",
        )
        (dot_codex / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "/usr/bin/existing-hook",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        return Installer(ROOT, home=home)

    @staticmethod
    def _install_legacy_resources(installer: Installer) -> tuple[bytes, bytes]:
        installer.cli_path.parent.mkdir(parents=True, exist_ok=True)
        cli = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(str(installer.python_executable))} "
            f"{shlex.quote(str(installer.paths.runner))} \"$@\"\n"
        ).encode("utf-8")
        installer.cli_path.write_bytes(cli)
        installer.legacy_launch_agent.parent.mkdir(parents=True, exist_ok=True)
        legacy_agent = plistlib.dumps(_legacy_launch_agent_payload(installer))
        installer.legacy_launch_agent.write_bytes(legacy_agent)
        return cli, legacy_agent

    def _assert_failed_install_is_safe(
        self,
        installer: Installer,
        *,
        original_cli: bytes,
        original_legacy_agent: bytes,
    ) -> None:
        config = installer_module.tomllib.loads(
            installer.config_file.read_text(encoding="utf-8")
        )
        self.assertFalse(
            installer_module._notify_references_runner(
                config.get("notify"), installer.paths.runner
            )
        )
        self.assertNotIn(
            str(installer.paths.runner),
            installer.hooks_file.read_text(encoding="utf-8"),
        )
        self.assertFalse(installer.launch_agent.exists())
        self.assertFalse(installer.paths.library_dir.exists())
        self.assertFalse(installer.paths.install_state.exists())
        self.assertFalse(installer.paths.pending_install_state.exists())
        self.assertEqual(installer.cli_path.read_bytes(), original_cli)
        self.assertEqual(
            installer.legacy_launch_agent.read_bytes(),
            original_legacy_agent,
        )
        if installer.paths.runner.exists():
            self.assertEqual(
                installer.paths.runner.read_text(encoding="utf-8"),
                installer_module.UNINSTALLED_RUNNER_CONTENT,
            )

    def test_install_fault_matrix_covers_every_filesystem_write_before_and_after(self):
        steps = (
            "prepare-runtime-directories",
            "publish-runtime",
            "publish-runner",
            "remove-legacy-cli",
            "remove-legacy-launch-agent",
            "publish-pending-install-state",
            "publish-notify-config",
            "publish-install-state",
            "remove-pending-install-state",
            "publish-hooks",
            "publish-current-launch-agent",
        )
        for step in steps:
            for phase in ("before", "after"):
                with self.subTest(step=step, phase=phase), tempfile.TemporaryDirectory() as directory:
                    installer = self._fixture(Path(directory))
                    original_cli, original_legacy_agent = (
                        self._install_legacy_resources(installer)
                    )

                    def inject(operation, observed_step, observed_phase):
                        if (operation, observed_step, observed_phase) == (
                            "install",
                            step,
                            phase,
                        ):
                            raise RuntimeError(f"fault:{step}:{phase}")

                    with patch.object(
                        installer,
                        "_transaction_checkpoint",
                        side_effect=inject,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "fault:"):
                            installer.install(start_agent=False)

                    self.assertEqual(
                        set(installer._ownership_snapshots),
                        {
                            "cli",
                            "current-launch-agent",
                            "legacy-launch-agent",
                            "hooks",
                            "notify",
                            "runtime",
                        },
                    )
                    self._assert_failed_install_is_safe(
                        installer,
                        original_cli=original_cli,
                        original_legacy_agent=original_legacy_agent,
                    )

    def test_uninstall_fault_matrix_restores_every_filesystem_write(self):
        steps = (
            "remove-legacy-cli",
            "remove-legacy-launch-agent",
            "restore-notify",
            "remove-hooks",
            "remove-current-launch-agent",
            "publish-safety-runner",
            "remove-install-state",
            "remove-pending-install-state",
            "remove-runtime",
            "purge-data",
            "purge-logs",
        )
        for step in steps:
            for phase in ("before", "after"):
                with self.subTest(step=step, phase=phase), tempfile.TemporaryDirectory() as directory:
                    installer = self._fixture(Path(directory))
                    installer.install(start_agent=False)
                    self._install_legacy_resources(installer)
                    tracked_paths = (
                        installer.config_file,
                        installer.hooks_file,
                        installer.paths.runner,
                        installer.paths.install_state,
                        installer.launch_agent,
                        installer.cli_path,
                        installer.legacy_launch_agent,
                    )
                    before = {path: path.read_bytes() for path in tracked_paths}
                    runtime_before = installer_module._directory_fingerprint(
                        installer.paths.library_dir
                    )

                    def inject(operation, observed_step, observed_phase):
                        if (operation, observed_step, observed_phase) == (
                            "uninstall",
                            step,
                            phase,
                        ):
                            raise RuntimeError(f"fault:{step}:{phase}")

                    with patch.object(
                        installer,
                        "_transaction_checkpoint",
                        side_effect=inject,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "fault:"):
                            installer.uninstall(purge=True)

                    for path, content in before.items():
                        self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(
                        installer_module._directory_fingerprint(
                            installer.paths.library_dir
                        ),
                        runtime_before,
                    )
                    self.assertTrue(installer.paths.data_dir.is_dir())
                    self.assertTrue(installer.paths.log_dir.is_dir())

    def test_rescue_uninstall_fault_matrix_restores_runner_and_runtime_root(self):
        for step in ("remove-runner", "remove-runtime-root"):
            for phase in ("before", "after"):
                with self.subTest(step=step, phase=phase), tempfile.TemporaryDirectory() as directory:
                    installer = self._fixture(Path(directory))
                    direct_config = installer.config_file.read_bytes()
                    installer.install(start_agent=False)
                    installer.config_file.write_bytes(direct_config)
                    installer.paths.install_state.unlink()
                    before_runner = installer.paths.runner.read_bytes()

                    def inject(operation, observed_step, observed_phase):
                        if (operation, observed_step, observed_phase) == (
                            "uninstall",
                            step,
                            phase,
                        ):
                            raise RuntimeError(f"fault:{step}:{phase}")

                    with patch.object(
                        installer,
                        "_transaction_checkpoint",
                        side_effect=inject,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "fault:"):
                            installer.uninstall(purge=True)

                    self.assertEqual(
                        installer.paths.runner.read_bytes(),
                        before_runner,
                    )
                    self.assertTrue(installer.paths.root.is_dir())
                    self.assertTrue(installer.paths.library_dir.is_dir())
                    self.assertTrue(installer.paths.data_dir.is_dir())
                    self.assertTrue(installer.paths.log_dir.is_dir())

    def test_install_rollback_does_not_overwrite_concurrent_hooks_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = self._fixture(Path(directory))
            self._install_legacy_resources(installer)
            replacement = b'{"hooks":{"External":[]}}\n'

            def inject(operation, step, phase):
                if (operation, step, phase) == (
                    "install",
                    "publish-hooks",
                    "after",
                ):
                    raise RuntimeError("fault after hooks")
                if (operation, step, phase) == (
                    "install",
                    "filesystem-publication",
                    "rollback-before",
                ):
                    installer.hooks_file.write_bytes(replacement)

            with patch.object(
                installer,
                "_transaction_checkpoint",
                side_effect=inject,
            ):
                with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                    installer.install(start_agent=False)

            self.assertEqual(installer.hooks_file.read_bytes(), replacement)

    def test_uninstall_rollback_does_not_overwrite_concurrent_launch_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = self._fixture(Path(directory))
            installer.install(start_agent=False)
            replacement = b"external plist"

            def inject(operation, step, phase):
                if (operation, step, phase) == (
                    "uninstall",
                    "remove-current-launch-agent",
                    "after",
                ):
                    raise RuntimeError("fault after launch agent")
                if (operation, step, phase) == (
                    "uninstall",
                    "remove-current-launch-agent",
                    "rollback-before",
                ):
                    installer.launch_agent.write_bytes(replacement)

            with patch.object(
                installer,
                "_transaction_checkpoint",
                side_effect=inject,
            ):
                with self.assertRaisesRegex(RuntimeError, "回滚不完整"):
                    installer.uninstall()

            self.assertEqual(installer.launch_agent.read_bytes(), replacement)

    def test_rollback_journal_rejects_undeclared_state_transition(self):
        journal = installer_module._RollbackJournal(operation="install")
        with self.assertRaisesRegex(RuntimeError, "未授权状态转换"):
            journal.record(
                step="overwrite-foreign-cli",
                resource="cli",
                transition=("foreign", "owned"),
                compensate=lambda: [],
            )


if __name__ == "__main__":
    unittest.main()
