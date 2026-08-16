import io
import json
import plistlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_notify import __version__
from codex_notify.cli import (
    _app_server_capabilities,
    _app_server_terminal_capability,
    _configure,
    _doctor,
    _hook_handler_ok,
    _status,
    main,
)
from codex_notify.computer_use import ComputerUseIntegration, encode_previous_notify
from codex_notify.constants import (
    HOOK_STATUS_PERMISSION,
    HOOK_STATUS_REQUEST_USER_INPUT,
    HOOK_STATUS_START,
)
from codex_notify.experimental_status import ExperimentalCapability
from codex_notify.installer import LEGACY_LAUNCH_AGENT_LABEL
from codex_notify.keychain import FeishuCredentials


class CliTests(unittest.TestCase):
    def test_legacy_hooks_are_accepted_as_noops(self):
        with patch("codex_notify.cli.NotificationStore") as store:
            self.assertEqual(main(["hook", "Stop"]), 0)
        store.assert_not_called()
        with patch("codex_notify.cli.hook_main", return_value=0) as hook_main:
            self.assertEqual(main(["hook", "PreToolUse"]), 0)
        hook_main.assert_called_once_with("PreToolUse")

    def test_install_command_uses_the_installed_package_directory(self):
        with (
            patch("codex_notify.cli.Installer") as installer_type,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["install", "--no-start"]), 0)

        package_dir = installer_type.call_args.args[0]
        self.assertEqual(package_dir.name, "codex_notify")
        installer_type.return_value.install.assert_called_once_with(start_agent=False)

    def test_uninstall_command_does_not_construct_notification_store(self):
        with (
            patch("codex_notify.cli.Installer") as installer_type,
            patch("codex_notify.cli.NotificationStore") as store,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["uninstall", "--purge"]), 0)

        installer_type.return_value.uninstall.assert_called_once_with(purge=True)
        store.assert_not_called()

    def test_status_has_no_private_reviewer_protocol_fields(self):
        store = Mock()
        store.status_snapshot.return_value = {
            "enabled": True,
            "active_turns": 1,
            "pending": 2,
            "pending_decisions": 1,
            "dead": 0,
            "last_delivery_at": None,
            "last_error": None,
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _status(store)
        report = output.getvalue().lower()
        self.assertNotIn("review", report)
        self.assertNotIn("launcher", report)
        self.assertNotIn("hmac", report)

    def test_status_shows_terminal_counters_without_raw_error(self):
        store = Mock()
        store.status_snapshot.return_value = {
            "enabled": True,
            "active_turns": 1,
            "pending": 0,
            "pending_decisions": 0,
            "waiting_terminal": 2,
            "completed_turns": 3,
            "failed_turns": 4,
            "interrupted_turns": 5,
            "permission_total": 6,
            "permission_sent": 5,
            "last_terminal_query_ok": "0",
            "last_terminal_query_at": "100",
            "dead": 0,
            "last_delivery_at": None,
            "last_error": "/private/path command=secret-token",
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _status(store)
        report = output.getvalue()
        self.assertIn("等待终态校准：2", report)
        self.assertIn("completed=3，failed=4，interrupted=5", report)
        self.assertIn("总计=6，已发送=5", report)
        self.assertIn("最近 App Server 终态查询：失败", report)
        self.assertNotIn("/private/path", report)
        self.assertNotIn("secret-token", report)

    def test_app_server_capability_checks_generated_terminal_schema(self):
        def generate(arguments, **_kwargs):
            root = Path(arguments[-1]) / "v2"
            root.mkdir(parents=True)
            (root / "ThreadTurnsListParams.json").write_text(
                json.dumps(
                    {
                        "definitions": {
                            "TurnItemsView": {
                                "oneOf": [{"enum": ["notLoaded"]}]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "ThreadTurnsListResponse.json").write_text(
                json.dumps(
                    {
                        "definitions": {
                            "TurnStatus": {
                                "enum": [
                                    "completed",
                                    "failed",
                                    "interrupted",
                                    "inProgress",
                                ]
                            },
                            "Turn": {
                                "properties": {
                                    key: {} for key in ("id", "status", "items", "itemsView")
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            return Mock(returncode=0)

        with patch("codex_notify.cli.subprocess.run", side_effect=generate):
            self.assertTrue(_app_server_terminal_capability(Path("/codex")))

    def test_app_server_capability_fails_closed_on_malformed_schema_shape(self):
        def generate(arguments, **_kwargs):
            root = Path(arguments[-1]) / "v2"
            root.mkdir(parents=True)
            (root / "ThreadTurnsListParams.json").write_text(
                json.dumps(
                    {
                        "definitions": {
                            "TurnItemsView": {"oneOf": [True]}
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "ThreadTurnsListResponse.json").write_text(
                json.dumps(
                    {
                        "definitions": {
                            "TurnStatus": {"enum": ["completed"]},
                            "Turn": {"properties": []},
                        }
                    }
                ),
                encoding="utf-8",
            )
            return Mock(returncode=0)

        with patch("codex_notify.cli.subprocess.run", side_effect=generate):
            self.assertFalse(_app_server_terminal_capability(Path("/codex")))

    def test_app_server_capability_fails_closed_on_non_utf8_schema(self):
        def generate(arguments, **_kwargs):
            root = Path(arguments[-1]) / "v2"
            root.mkdir(parents=True)
            (root / "ThreadTurnsListParams.json").write_bytes(b"\xff")
            return Mock(returncode=0)

        with patch("codex_notify.cli.subprocess.run", side_effect=generate):
            self.assertFalse(_app_server_terminal_capability(Path("/codex")))

    def test_app_server_capabilities_generate_schema_once(self):
        capabilities = {
            feature: ExperimentalCapability(True, "fixture")
            for feature in ("request-user-input", "mcp-auth", "rate-limits")
        }
        with (
            patch(
                "codex_notify.cli._app_server_terminal_capability_from_schema",
                return_value=True,
            ) as terminal_parser,
            patch(
                "codex_notify.cli.read_experimental_capabilities",
                return_value=capabilities,
            ) as experimental_parser,
            patch(
                "codex_notify.cli.subprocess.run", return_value=Mock(returncode=0)
            ) as run,
        ):
            terminal, experimental = _app_server_capabilities(Path("/codex"))

        self.assertTrue(terminal)
        self.assertEqual(experimental, capabilities)
        run.assert_called_once()
        terminal_parser.assert_called_once()
        experimental_parser.assert_called_once()

    def test_hook_handler_requires_command_type(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "runner.py"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            runner.chmod(0o700)
            handler = {
                "command": f'"{sys.executable}" "{runner}" hook UserPromptSubmit'
            }

            self.assertFalse(_hook_handler_ok(handler, runner))

    def test_hook_handler_rejects_managed_metadata_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "runner.py"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            runner.chmod(0o700)
            command = f'"{sys.executable}" "{runner}" hook UserPromptSubmit'
            expected = {
                "type": "command",
                "command": command,
                "timeout": 5,
                "statusMessage": HOOK_STATUS_START,
            }
            self.assertTrue(_hook_handler_ok(expected, runner))
            for drifted in (
                {**expected, "timeout": 99},
                {key: value for key, value in expected.items() if key != "statusMessage"},
                {**expected, "unexpected": True},
            ):
                self.assertFalse(_hook_handler_ok(drifted, runner))

    def test_configure_validates_all_input_before_writing_keychain(self):
        with (
            patch("codex_notify.cli.platform.system", return_value="Darwin"),
            patch(
                "codex_notify.cli.prompt_secret",
                side_effect=["https://example.com/not-feishu", "secret"],
            ),
            patch("codex_notify.cli.store_credentials") as store_credentials,
        ):
            with self.assertRaises(ValueError):
                _configure()

        store_credentials.assert_not_called()

    def test_test_command_targets_the_event_it_created(self):
        store = Mock()
        store.enqueue_test.return_value = "target-event"
        store.event_status.return_value = "sent"

        with (
            patch("codex_notify.cli.NotificationStore", return_value=store),
            patch("codex_notify.cli.run_once", return_value=1) as run_once,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["test"]), 0)

        run_once.assert_called_once_with(store, event_key="target-event")

    def test_on_command_only_promises_confirmed_root_notifications(self):
        store = Mock()
        output = io.StringIO()

        with (
            patch("codex_notify.cli.NotificationStore", return_value=store),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["on"]), 0)

        store.set_enabled.assert_called_once_with(True)
        self.assertIn("已确认的用户根 Turn", output.getvalue())
        self.assertNotIn("每个 Codex Turn", output.getvalue())

    def test_experimental_enable_probes_capability_and_remains_independent(self):
        store = Mock()
        capabilities = {
            feature: ExperimentalCapability(True, "fixture")
            for feature in ("request-user-input", "mcp-auth", "rate-limits")
        }
        with (
            patch("codex_notify.cli.NotificationStore", return_value=store),
            patch(
                "codex_notify.cli.probe_experimental_capabilities",
                return_value=capabilities,
            ),
            patch("codex_notify.cli.find_bundled_codex", return_value=Path("/codex")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["experimental", "enable", "request-user-input"]), 0
            )
        store.set_enabled.assert_not_called()
        store.set_experimental_enabled.assert_called_once_with(
            "request-user-input", True
        )
        store.set_experimental_capability.assert_called_once_with(
            "request-user-input", True, "fixture"
        )

    def test_experimental_enable_probe_failure_preserves_existing_state(self):
        store = Mock()
        capabilities = {
            feature: ExperimentalCapability(
                feature != "mcp-auth", "fixture" if feature != "mcp-auth" else "transient"
            )
            for feature in ("request-user-input", "mcp-auth", "rate-limits")
        }
        with (
            patch("codex_notify.cli.NotificationStore", return_value=store),
            patch(
                "codex_notify.cli.probe_experimental_capabilities",
                return_value=capabilities,
            ),
            patch("codex_notify.cli.find_bundled_codex", return_value=Path("/codex")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["experimental", "enable", "mcp-auth"]), 1)

        store.set_experimental_capability.assert_not_called()
        store.set_experimental_enabled.assert_not_called()

    def test_experimental_status_probe_is_read_only(self):
        store = Mock()
        store.experimental_feature_status.return_value = {
            feature: {"enabled": feature == "mcp-auth"}
            for feature in ("request-user-input", "mcp-auth", "rate-limits")
        }
        capabilities = {
            feature: ExperimentalCapability(False, "transient")
            for feature in ("request-user-input", "mcp-auth", "rate-limits")
        }
        output = io.StringIO()
        with (
            patch("codex_notify.cli.NotificationStore", return_value=store),
            patch(
                "codex_notify.cli.probe_experimental_capabilities",
                return_value=capabilities,
            ),
            patch("codex_notify.cli.find_bundled_codex", return_value=Path("/codex")),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["experimental", "status"]), 0)

        store.set_experimental_capability.assert_not_called()
        store.set_experimental_enabled.assert_not_called()
        self.assertIn("mcp-auth：开启；unavailable；transient", output.getvalue())

    def test_experimental_disable_does_not_probe_or_change_total_switch(self):
        store = Mock()
        with (
            patch("codex_notify.cli.NotificationStore", return_value=store),
            patch("codex_notify.cli.probe_experimental_capabilities") as probe,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["experimental", "disable", "mcp-auth"]), 0)
        probe.assert_not_called()
        store.set_experimental_enabled.assert_called_once_with("mcp-auth", False)
        store.set_enabled.assert_not_called()

    def test_doctor_rejects_marker_strings_and_unloaded_launch_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks_file = root / "hooks.json"
            config_file = root / "config.toml"
            launch_agent = root / "agent.plist"
            runner = root / "runner.py"
            library_dir = root / "lib"
            hooks_file.write_text(
                json.dumps({"description": "codex-notify/runner.py", "hooks": {}}),
                encoding="utf-8",
            )
            config_file.write_text(
                'banner = """codex-notify managed notification"""\n',
                encoding="utf-8",
            )
            with launch_agent.open("wb") as handle:
                plistlib.dump({"Label": "wrong"}, handle)
            runner.write_text("", encoding="utf-8")
            paths = SimpleNamespace(
                hooks_file=hooks_file,
                config_file=config_file,
                launch_agent=launch_agent,
                runner=runner,
                library_dir=library_dir,
            )

            with (
                patch("codex_notify.cli.platform.system", return_value="Darwin"),
                patch(
                    "codex_notify.cli.load_credentials",
                    return_value=FeishuCredentials(
                        "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                        "secret",
                    ),
                ),
                patch("codex_notify.cli.subprocess.run") as run,
                redirect_stdout(io.StringIO()),
            ):
                run.return_value.returncode = 1
                self.assertEqual(_doctor(paths), 1)

    def test_doctor_fails_when_runtime_entrypoint_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks_file = root / "hooks.json"
            config_file = root / "config.toml"
            launch_agent = root / "agent.plist"
            runner = root / "missing-runner.py"
            library_dir = root / "missing-lib"
            hooks_file.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "command": f"python {runner} hook UserPromptSubmit"
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_file.write_text(
                f'notify = ["python", "{runner}", "notify"]\n',
                encoding="utf-8",
            )
            with launch_agent.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": "com.guang.codex-turn-notifier",
                        "ProgramArguments": ["python", str(runner), "worker", "--once"],
                    },
                    handle,
                )
            paths = SimpleNamespace(
                hooks_file=hooks_file,
                config_file=config_file,
                launch_agent=launch_agent,
                runner=runner,
                library_dir=library_dir,
            )
            output = io.StringIO()

            with (
                patch("codex_notify.cli.platform.system", return_value="Darwin"),
                patch(
                    "codex_notify.cli.load_credentials",
                    return_value=FeishuCredentials(
                        "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                        "secret",
                    ),
                ),
                patch("codex_notify.cli.subprocess.run") as run,
                redirect_stdout(output),
            ):
                run.return_value.returncode = 0
                self.assertEqual(_doctor(paths), 1)

            self.assertIn("运行入口", output.getvalue())

    def test_doctor_rejects_non_dictionary_launch_agent_plist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launch_agent = root / "agent.plist"
            with launch_agent.open("wb") as handle:
                plistlib.dump(["valid plist", "wrong root type"], handle)
            paths = SimpleNamespace(
                hooks_file=root / "hooks.json",
                config_file=root / "config.toml",
                launch_agent=launch_agent,
                runner=root / "runner.py",
                library_dir=root / "lib",
            )

            with (
                patch("codex_notify.cli.platform.system", return_value="Darwin"),
                patch("codex_notify.cli.load_credentials", side_effect=RuntimeError("missing")),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(_doctor(paths), 1)

    def test_doctor_rejects_commands_that_only_contain_runner_as_an_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "runner.py"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            runner.chmod(0o700)
            package_dir = root / "lib" / "codex_notify"
            package_dir.mkdir(parents=True)
            (package_dir / "__init__.py").write_text(
                f"__version__ = {__version__!r}\n", encoding="utf-8"
            )
            hooks_file = root / "hooks.json"
            hooks_file.write_text(
                json.dumps(
                    {
                        "hooks": {
                            event_name: [
                                {
                                    **(
                                        {"matcher": ".*"}
                                        if event_name == "PermissionRequest"
                                        else (
                                            {"matcher": "^request_user_input$"}
                                            if event_name == "PreToolUse"
                                            else {}
                                        )
                                    ),
                                    "hooks": [
                                        {
                                            "command": (
                                                f'"{sys.executable}" "{runner}.old" '
                                                f"hook {event_name}"
                                            )
                                        }
                                    ]
                                }
                            ]
                            for event_name in (
                                "SessionStart",
                                "UserPromptSubmit",
                                "SubagentStart",
                                "SubagentStop",
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_file = root / "config.toml"
            config_file.write_text(
                f'notify = ["/usr/bin/true", "{runner}", "notify"]\n',
                encoding="utf-8",
            )
            launch_agent = root / "agent.plist"
            with launch_agent.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": "com.guang.codex-turn-notifier",
                        "RunAtLoad": True,
                        "StartInterval": 10,
                        "ProgramArguments": [
                            "/usr/bin/true",
                            str(runner),
                            "worker",
                            "--once",
                        ],
                    },
                    handle,
                )
            paths = SimpleNamespace(
                hooks_file=hooks_file,
                config_file=config_file,
                launch_agent=launch_agent,
                runner=runner,
                library_dir=root / "lib",
            )
            output = io.StringIO()

            with (
                patch("codex_notify.cli.platform.system", return_value="Darwin"),
                patch(
                    "codex_notify.cli.load_credentials",
                    return_value=FeishuCredentials(
                        "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                        "secret",
                    ),
                ),
                patch("codex_notify.cli.subprocess.run") as run,
                redirect_stdout(output),
            ):
                run.return_value.returncode = 0
                self.assertEqual(_doctor(paths), 1)

            report = output.getvalue()
            self.assertIn("✗ Codex Hooks", report)
            self.assertIn("✗ Computer Use 通知链", report)
            self.assertIn("✗ LaunchAgent", report)

    def test_doctor_accepts_exact_installed_command_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "runner.py"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            runner.chmod(0o700)
            package_dir = root / "lib" / "codex_notify"
            package_dir.mkdir(parents=True)
            (package_dir / "__init__.py").write_text(
                f"__version__ = {__version__!r}\n", encoding="utf-8"
            )
            hooks_file = root / "hooks.json"
            hooks_file.write_text(
                json.dumps(
                    {
                        "hooks": {
                            event_name: [
                                {
                                    **(
                                        {"matcher": ".*"}
                                        if event_name == "PermissionRequest"
                                        else (
                                            {"matcher": "^request_user_input$"}
                                            if event_name == "PreToolUse"
                                            else {}
                                        )
                                    ),
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f'"{sys.executable}" "{runner}" '
                                                f"hook {event_name}"
                                            ),
                                            "timeout": 5,
                                            **(
                                                {"statusMessage": HOOK_STATUS_START}
                                                if event_name == "UserPromptSubmit"
                                                else (
                                                    {"statusMessage": HOOK_STATUS_PERMISSION}
                                                    if event_name == "PermissionRequest"
                                                    else (
                                                        {"statusMessage": HOOK_STATUS_REQUEST_USER_INPUT}
                                                        if event_name == "PreToolUse"
                                                        else {}
                                                    )
                                                )
                                            ),
                                        }
                                    ]
                                }
                            ]
                            for event_name in (
                                "SessionStart",
                                "UserPromptSubmit",
                                "SubagentStart",
                                "SubagentStop",
                                "PermissionRequest",
                                "PreToolUse",
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_file = root / "config.toml"
            computer_use = root / "SkyComputerUseClient"
            computer_use.write_text("", encoding="utf-8")
            computer_use.chmod(0o700)
            previous = [sys.executable, str(runner), "notify"]
            notify = [
                str(computer_use),
                "turn-ended",
                "--previous-notify",
                encode_previous_notify(previous),
            ]
            config_file.write_text(
                "notify = " + json.dumps(notify) + "\n",
                encoding="utf-8",
            )
            install_state = root / "install-state.json"
            install_state.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "runtime_version": __version__,
                        "computer_use": {
                            "executable": str(computer_use),
                            "bundle_id": "com.openai.sky.CUAService.cli",
                            "team_id": "2DC432GLL2",
                            "version": "26.804.1000633",
                            "signature_verified": True,
                        },
                        "original_notify": [str(computer_use), "turn-ended"],
                        "original_notify_source": (
                            "notify = "
                            + json.dumps([str(computer_use), "turn-ended"])
                            + "\n"
                        ),
                        "installed_notify": notify,
                        "previous_notify": previous,
                    }
                ),
                encoding="utf-8",
            )
            install_state.chmod(0o600)
            launch_agent = root / "agent.plist"
            with launch_agent.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": "io.github.guangcodes.codex-notify",
                        "RunAtLoad": True,
                        "StartInterval": 10,
                        "ProgramArguments": [
                            sys.executable,
                            str(runner),
                            "worker",
                            "--once",
                        ],
                    },
                    handle,
                )
            paths = SimpleNamespace(
                hooks_file=hooks_file,
                config_file=config_file,
                launch_agent=launch_agent,
                legacy_launch_agent=root / "legacy-agent.plist",
                runner=runner,
                library_dir=root / "lib",
                install_state=install_state,
            )

            def launchctl_status(arguments, **_kwargs):
                if LEGACY_LAUNCH_AGENT_LABEL in arguments[-1]:
                    return Mock(
                        returncode=113,
                        stdout="",
                        stderr=(
                            "Could not find service "
                            f'\"{LEGACY_LAUNCH_AGENT_LABEL}\" in domain'
                        ),
                    )
                return Mock(returncode=0, stdout="", stderr="")

            with (
                patch("codex_notify.cli.platform.system", return_value="Darwin"),
                patch(
                    "codex_notify.cli.load_credentials",
                    return_value=FeishuCredentials(
                        "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                        "secret",
                    ),
                ),
                patch(
                    "codex_notify.cli.inspect_computer_use",
                    return_value=ComputerUseIntegration(
                        executable=computer_use,
                        version="26.804.1000633",
                        signature_verified=True,
                        notify=tuple(notify),
                        previous_notify=tuple(previous),
                    ),
                ),
                patch(
                    "codex_notify.cli._app_server_capabilities",
                    return_value=(True, {
                        feature: ExperimentalCapability(True, "fixture")
                        for feature in (
                            "request-user-input",
                            "mcp-auth",
                            "rate-limits",
                        )
                    }),
                ),
                patch("codex_notify.cli.subprocess.run") as run,
                redirect_stdout(io.StringIO()),
            ):
                run.side_effect = launchctl_status
                self.assertEqual(_doctor(paths), 0)
                run.side_effect = None
                run.return_value.returncode = 0
                legacy_output = io.StringIO()
                with redirect_stdout(legacy_output):
                    self.assertEqual(_doctor(paths), 1)
                self.assertIn("仍在运行", legacy_output.getvalue())
                run.return_value.returncode = 5
                run.return_value.stdout = ""
                run.return_value.stderr = "launchd unavailable"
                query_error_output = io.StringIO()
                with redirect_stdout(query_error_output):
                    self.assertEqual(_doctor(paths), 1)
                self.assertIn(
                    "无法确认旧 LaunchAgent 状态：launchd unavailable",
                    query_error_output.getvalue(),
                )
                run.side_effect = launchctl_status
                paths.legacy_launch_agent.symlink_to(root / "missing-legacy.plist")
                symlink_output = io.StringIO()
                with redirect_stdout(symlink_output):
                    self.assertEqual(_doctor(paths), 1)
                self.assertIn("旧 plist", symlink_output.getvalue())
                paths.legacy_launch_agent.unlink()
                install_state.unlink()
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(_doctor(paths), 1)

            self.assertIn("安装状态", output.getvalue())
            self.assertIn(
                f"✓ Computer Use：{computer_use}（版本 26.804.1000633",
                output.getvalue(),
            )
            self.assertIn("✗ codex-notify 安装状态：缺少", output.getvalue())

    def test_doctor_rejects_explicitly_disabled_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "runner.py"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            runner.chmod(0o700)
            package_dir = root / "lib" / "codex_notify"
            package_dir.mkdir(parents=True)
            (package_dir / "__init__.py").write_text(
                f"__version__ = {__version__!r}\n", encoding="utf-8"
            )
            hooks_file = root / "hooks.json"
            hooks_file.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f'"{sys.executable}" "{runner}" '
                                                "hook UserPromptSubmit"
                                            ),
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_file = root / "config.toml"
            config_file.write_text(
                "notify = "
                + json.dumps([sys.executable, str(runner), "notify"])
                + "\n\n[features]\nhooks = false\n",
                encoding="utf-8",
            )
            launch_agent = root / "agent.plist"
            with launch_agent.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": "com.guang.codex-turn-notifier",
                        "RunAtLoad": True,
                        "StartInterval": 10,
                        "ProgramArguments": [
                            sys.executable,
                            str(runner),
                            "worker",
                            "--once",
                        ],
                    },
                    handle,
                )
            paths = SimpleNamespace(
                hooks_file=hooks_file,
                config_file=config_file,
                launch_agent=launch_agent,
                runner=runner,
                library_dir=root / "lib",
            )
            output = io.StringIO()

            with (
                patch("codex_notify.cli.platform.system", return_value="Darwin"),
                patch(
                    "codex_notify.cli.load_credentials",
                    return_value=FeishuCredentials(
                        "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                        "secret",
                    ),
                ),
                patch("codex_notify.cli.subprocess.run") as run,
                redirect_stdout(output),
            ):
                run.return_value.returncode = 0
                self.assertEqual(_doctor(paths), 1)

            self.assertIn("Hooks 已在 config.toml 中关闭", output.getvalue())

    def test_doctor_rejects_launch_agent_without_recurring_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "runner.py"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            runner.chmod(0o700)
            package_dir = root / "lib" / "codex_notify"
            package_dir.mkdir(parents=True)
            (package_dir / "__init__.py").write_text(
                f"__version__ = {__version__!r}\n", encoding="utf-8"
            )
            hooks_file = root / "hooks.json"
            hooks_file.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f'"{sys.executable}" "{runner}" '
                                                "hook UserPromptSubmit"
                                            ),
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_file = root / "config.toml"
            config_file.write_text(
                "notify = "
                + json.dumps([sys.executable, str(runner), "notify"])
                + "\n",
                encoding="utf-8",
            )
            launch_agent = root / "agent.plist"
            with launch_agent.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": "com.guang.codex-turn-notifier",
                        "RunAtLoad": False,
                        "ProgramArguments": [
                            sys.executable,
                            str(runner),
                            "worker",
                            "--once",
                        ],
                    },
                    handle,
                )
            paths = SimpleNamespace(
                hooks_file=hooks_file,
                config_file=config_file,
                launch_agent=launch_agent,
                runner=runner,
                library_dir=root / "lib",
            )

            with (
                patch("codex_notify.cli.platform.system", return_value="Darwin"),
                patch(
                    "codex_notify.cli.load_credentials",
                    return_value=FeishuCredentials(
                        "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                        "secret",
                    ),
                ),
                patch("codex_notify.cli.subprocess.run") as run,
                redirect_stdout(io.StringIO()),
            ):
                run.return_value.returncode = 0
                self.assertEqual(_doctor(paths), 1)


if __name__ == "__main__":
    unittest.main()
