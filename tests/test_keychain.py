import json
import sys
import unittest
from base64 import b64encode
from subprocess import TimeoutExpired
from unittest.mock import Mock, patch

from codex_notify.constants import (
    KEYCHAIN_CREDENTIALS_SERVICE,
    KEYCHAIN_SECRET_SERVICE,
    KEYCHAIN_WEBHOOK_SERVICE,
)
from codex_notify.keychain import (
    FeishuCredentials,
    KeychainError,
    KeychainItemNotFound,
    ERR_SEC_DUPLICATE_ITEM,
    ERR_SEC_INTERACTION_NOT_ALLOWED,
    ERR_SEC_ITEM_NOT_FOUND,
    _KeychainInteractionNotAllowed,
    _NativeReadFallback,
    _copy_keychain_item_data,
    _read_native_keychain_item,
    _read_native_keychain_item_bounded,
    _set_password_via_security_framework,
    _upsert_keychain_item,
    _write_native_keychain_item,
    get_password,
    load_credentials,
    store_credentials,
)


class KeychainTests(unittest.TestCase):
    def test_keychain_read_uses_bounded_native_subprocess(self):
        with patch(
            "codex_notify.keychain._read_native_keychain_item_bounded",
            return_value=b"value",
        ) as read:
            self.assertEqual(get_password("service"), "value")

        read.assert_called_once_with("service", "codex-notify")

    def test_keychain_access_failure_is_not_reported_as_missing(self):
        with (
            patch(
                "codex_notify.keychain._read_native_keychain_item_bounded",
                side_effect=KeychainError("原生子进程退出码 1"),
            ),
            patch("codex_notify.keychain._read_password_via_security_cli") as fallback,
        ):
            with self.assertRaisesRegex(KeychainError, "退出码 1") as raised:
                get_password("service")

        self.assertNotIn("缺少", str(raised.exception))
        fallback.assert_not_called()

    def test_keychain_read_falls_back_after_bounded_native_timeout(self):
        with (
            patch(
                "codex_notify.keychain._read_native_keychain_item_bounded",
                side_effect=_NativeReadFallback("timeout"),
            ),
            patch(
                "codex_notify.keychain._read_password_via_security_cli",
                return_value="legacy-value",
            ) as fallback,
        ):
            self.assertEqual(get_password("service"), "legacy-value")

        fallback.assert_called_once_with("service")

    def test_keychain_read_timeout_is_reported(self):
        with (
            patch(
                "codex_notify.keychain._read_native_keychain_item_bounded",
                side_effect=_NativeReadFallback("timeout"),
            ),
            patch(
                "codex_notify.keychain.subprocess.run",
                side_effect=TimeoutExpired(cmd="security", timeout=15),
            ),
        ):
            with self.assertRaisesRegex(KeychainError, "读取.*超时"):
                get_password("service")

    def test_keychain_read_rejects_empty_data(self):
        with patch(
            "codex_notify.keychain._read_native_keychain_item_bounded",
            return_value=b"",
        ):
            with self.assertRaisesRegex(KeychainError, "为空"):
                get_password("service")

    def test_keychain_read_rejects_invalid_utf8(self):
        with patch(
            "codex_notify.keychain._read_native_keychain_item_bounded",
            return_value=b"\xff",
        ):
            with self.assertRaisesRegex(KeychainError, "UTF-8"):
                get_password("service")

    def test_native_keychain_writer_preserves_values_larger_than_prompt_limit(self):
        value = "凭据" * 100
        with patch("codex_notify.keychain._write_native_keychain_item") as write:
            _set_password_via_security_framework("service", "label", value)

        service, account, label, encoded = write.call_args.args
        self.assertEqual(service, "service")
        self.assertEqual(account, "codex-notify")
        self.assertEqual(label, "label")
        self.assertEqual(encoded.decode("utf-8"), value)
        self.assertGreater(len(encoded), 128)

    def test_native_keychain_upsert_updates_existing_item(self):
        security = Mock()
        security.SecItemUpdate.return_value = 0

        _upsert_keychain_item(security, 1, 2, 3)

        security.SecItemUpdate.assert_called_once_with(1, 2)
        security.SecItemAdd.assert_not_called()

    def test_native_keychain_upsert_adds_missing_item(self):
        security = Mock()
        security.SecItemUpdate.return_value = ERR_SEC_ITEM_NOT_FOUND
        security.SecItemAdd.return_value = 0

        _upsert_keychain_item(security, 1, 2, 3)

        security.SecItemAdd.assert_called_once_with(3, None)

    def test_native_keychain_upsert_recovers_from_concurrent_insert(self):
        security = Mock()
        security.SecItemUpdate.side_effect = [ERR_SEC_ITEM_NOT_FOUND, 0]
        security.SecItemAdd.return_value = ERR_SEC_DUPLICATE_ITEM

        _upsert_keychain_item(security, 1, 2, 3)

        self.assertEqual(security.SecItemUpdate.call_count, 2)

    def test_native_keychain_upsert_reports_security_status(self):
        security = Mock()
        security.SecItemUpdate.return_value = -50

        with self.assertRaisesRegex(KeychainError, "状态 -50"):
            _upsert_keychain_item(security, 1, 2, 3)

    def test_bounded_native_reader_decodes_child_output_without_secret_argv(self):
        encoded = b64encode(b"value")
        process = Mock(returncode=0)
        process.communicate.return_value = (encoded, b"")
        with patch(
            "codex_notify.keychain.subprocess.Popen",
            return_value=process,
        ) as popen:
            self.assertEqual(
                _read_native_keychain_item_bounded("service", "account"),
                b"value",
            )

        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], [sys.executable, "-P", "-c"])
        self.assertEqual(argv[-2:], ["service", "account"])
        self.assertNotIn("value", argv)
        process.communicate.assert_called_once_with(timeout=5)

    def test_bounded_native_reader_times_out_to_compatibility_path(self):
        process = Mock()
        process.communicate.side_effect = TimeoutExpired(cmd="python", timeout=5)
        with (
            patch("codex_notify.keychain.subprocess.Popen", return_value=process),
            patch("codex_notify.keychain.Thread") as thread,
        ):
            with self.assertRaises(_NativeReadFallback):
                _read_native_keychain_item_bounded("service", "account")

        process.kill.assert_called_once()
        thread.assert_called_once_with(target=process.wait, daemon=True)
        thread.return_value.start.assert_called_once()

    def test_bounded_native_reader_preserves_missing_item(self):
        process = Mock(returncode=44)
        process.communicate.return_value = (b"", b"")
        with patch(
            "codex_notify.keychain.subprocess.Popen",
            return_value=process,
        ):
            with self.assertRaises(KeychainItemNotFound):
                _read_native_keychain_item_bounded("service", "account")

    def test_native_keychain_reader_returns_data_and_releases_result(self):
        security = Mock()
        core_foundation = Mock()

        def copy_matching(_query, result):
            result._obj.value = 1234
            return 0

        security.SecItemCopyMatching.side_effect = copy_matching
        core_foundation.CFDataGetLength.return_value = 5
        core_foundation.CFDataGetBytePtr.return_value = 5678
        with patch("codex_notify.keychain.ctypes.string_at", return_value=b"value"):
            self.assertEqual(
                _copy_keychain_item_data(security, core_foundation, 1),
                b"value",
            )

        core_foundation.CFRelease.assert_called_once_with(1234)

    def test_native_keychain_reader_distinguishes_interaction_denial(self):
        security = Mock()
        security.SecItemCopyMatching.return_value = ERR_SEC_INTERACTION_NOT_ALLOWED

        with self.assertRaises(_KeychainInteractionNotAllowed):
            _copy_keychain_item_data(security, Mock(), 1)

    def test_native_keychain_reader_builds_fail_without_ui_query(self):
        security = Mock()
        core_foundation = Mock()
        arena = Mock()
        arena.__enter__ = Mock(return_value=arena)
        arena.__exit__ = Mock(return_value=None)
        arena.string.side_effect = [101, 102]
        arena.dictionary.return_value = 999
        constants = {
            "kSecClass": 1,
            "kSecClassGenericPassword": 2,
            "kSecAttrService": 3,
            "kSecAttrAccount": 4,
            "kSecReturnData": 5,
            "kSecMatchLimit": 6,
            "kSecMatchLimitOne": 7,
            "kSecUseAuthenticationUI": 8,
            "kSecUseAuthenticationUIFail": 9,
            "kCFBooleanTrue": 10,
        }

        with (
            patch(
                "codex_notify.keychain._load_native_frameworks",
                return_value=(security, core_foundation),
            ),
            patch(
                "codex_notify.keychain._security_constant",
                side_effect=lambda _framework, name: constants[name],
            ),
            patch("codex_notify.keychain._CFObjectArena", return_value=arena),
            patch(
                "codex_notify.keychain._copy_keychain_item_data",
                return_value=b"secret",
            ) as copy,
        ):
            self.assertEqual(
                _read_native_keychain_item("service", "account"),
                b"secret",
            )

        copy.assert_called_once()
        query_pairs = arena.dictionary.call_args.args[0]
        self.assertIn((8, 9), query_pairs)

    def test_loads_atomic_credentials_bundle(self):
        payload = json.dumps(
            {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                "signing_secret": "secret",
            }
        )
        with patch("codex_notify.keychain.get_password", return_value=payload) as get_password:
            credentials = load_credentials()

        self.assertEqual(
            credentials,
            FeishuCredentials(
                "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                "secret",
            ),
        )
        get_password.assert_called_once_with(KEYCHAIN_CREDENTIALS_SERVICE)

    def test_falls_back_to_legacy_split_credentials(self):
        def lookup(service):
            if service == KEYCHAIN_CREDENTIALS_SERVICE:
                raise KeychainItemNotFound("missing")
            if service == KEYCHAIN_WEBHOOK_SERVICE:
                return "https://open.feishu.cn/open-apis/bot/v2/hook/example"
            if service == KEYCHAIN_SECRET_SERVICE:
                return "legacy-secret"
            raise AssertionError(service)

        with patch("codex_notify.keychain.get_password", side_effect=lookup):
            credentials = load_credentials()

        self.assertEqual(credentials.signing_secret, "legacy-secret")

    def test_stores_both_credentials_in_one_keychain_item(self):
        credentials = FeishuCredentials(
            "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 80,
            "s" * 64,
        )
        with patch("codex_notify.keychain._set_password_via_security_framework") as setter:
            store_credentials(credentials)

        setter.assert_called_once()
        service, label, payload = setter.call_args.args
        self.assertEqual(service, KEYCHAIN_CREDENTIALS_SERVICE)
        self.assertIn("Codex Notify", label)
        self.assertEqual(json.loads(payload)["signing_secret"], "s" * 64)
        self.assertGreater(len(payload.encode("utf-8")), 128)


if __name__ == "__main__":
    unittest.main()
