import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_notify.computer_use import (
    COMPUTER_USE_BUNDLE_ID,
    COMPUTER_USE_TEAM_ID,
    decode_previous_notify,
    encode_previous_notify,
    inspect_computer_use,
)


class ComputerUseTests(unittest.TestCase):
    def test_previous_notify_codec_round_trips_without_shell_parsing(self):
        arguments = ["/path with spaces/python", "/tmp/runner.py", "notify"]

        encoded = encode_previous_notify(arguments)

        self.assertEqual(json.loads(encoded), arguments)
        self.assertEqual(decode_previous_notify(encoded), tuple(arguments))

    def test_previous_notify_codec_rejects_non_command_values(self):
        for value in ('"command"', "[]", '["ok", 3]', '["ok", ""]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_previous_notify(value)

    def test_inspection_validates_identity_capability_and_existing_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = (
                Path(directory)
                / "SkyComputerUseClient.app"
                / "Contents"
                / "MacOS"
                / "SkyComputerUseClient"
            )
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o700)
            with (executable.parents[1] / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": COMPUTER_USE_BUNDLE_ID,
                        "CFBundleShortVersionString": "26.804.1000633",
                    },
                    handle,
                )
            previous = ["/usr/bin/python3", "/tmp/runner.py", "notify"]
            notify = [
                str(executable),
                "turn-ended",
                "--previous-notify",
                encode_previous_notify(previous),
            ]

            with (
                patch(
                    "codex_notify.computer_use._verify_codesign", return_value=True
                ) as codesign,
                patch(
                    "codex_notify.computer_use._verify_previous_notify_support"
                ) as capability,
            ):
                result = inspect_computer_use(notify)

            self.assertEqual(result.previous_notify, tuple(previous))
            self.assertEqual(result.version, "26.804.1000633")
            self.assertTrue(result.signature_verified)
            codesign.assert_called_once_with(
                executable.parents[2], "26.804.1000633"
            )
            capability.assert_called_once_with(executable)

    def test_codesign_requires_openai_identifier_and_team(self):
        verified = Mock(returncode=0, stdout="", stderr="")
        details = Mock(
            returncode=0,
            stdout="",
            stderr=(
                f"Identifier={COMPUTER_USE_BUNDLE_ID}\n"
                f"TeamIdentifier={COMPUTER_USE_TEAM_ID}\n"
            ),
        )
        with patch(
            "codex_notify.computer_use.subprocess.run",
            side_effect=[verified, details],
        ) as run:
            from codex_notify.computer_use import _verify_codesign

            self.assertTrue(
                _verify_codesign(
                    Path("/Applications/SkyComputerUseClient.app"),
                    "26.804.1000633",
                )
            )

        self.assertEqual(run.call_count, 2)
        verify_arguments = run.call_args_list[0].args[0]
        self.assertTrue(
            any(
                argument.startswith("-R=anchor apple generic")
                for argument in verify_arguments
            )
        )
        self.assertTrue(
            any("certificate leaf[subject.OU]" in argument for argument in verify_arguments)
        )

    def test_failed_codesign_requires_an_exact_bundle_allowlist_match(self):
        with tempfile.TemporaryDirectory() as directory:
            client_app = Path(directory) / "SkyComputerUseClient.app"
            executable = client_app / "Contents" / "MacOS" / "SkyComputerUseClient"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"known packaged binary")
            (client_app / "Contents" / "Info.plist").write_bytes(b"known plist")
            verified = Mock(returncode=1, stdout="", stderr="invalid signature")
            details = Mock(
                returncode=0,
                stdout="",
                stderr=(
                    f"Identifier={COMPUTER_USE_BUNDLE_ID}\n"
                    f"TeamIdentifier={COMPUTER_USE_TEAM_ID}\n"
                ),
            )
            from codex_notify.computer_use import _bundle_digest, _verify_codesign

            digest = _bundle_digest(client_app)
            with (
                patch(
                    "codex_notify.computer_use.subprocess.run",
                    side_effect=[verified, details],
                ),
                patch.dict(
                    "codex_notify.computer_use.KNOWN_UNVERIFIED_BUNDLE_DIGESTS",
                    {"test-version": digest},
                    clear=True,
                ),
            ):
                self.assertFalse(_verify_codesign(client_app, "test-version"))

            with (
                patch(
                    "codex_notify.computer_use.subprocess.run",
                    side_effect=[verified, details],
                ),
                patch.dict(
                    "codex_notify.computer_use.KNOWN_UNVERIFIED_BUNDLE_DIGESTS",
                    {},
                    clear=True,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "不在兼容白名单"):
                    _verify_codesign(client_app, "test-version")


if __name__ == "__main__":
    unittest.main()
