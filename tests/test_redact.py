import base64
import time
import unittest
from unittest.mock import patch

from codex_notify.redact import safe_summary


class SafeSummaryTests(unittest.TestCase):
    def test_compacts_and_truncates(self):
        self.assertEqual(safe_summary(" a\n b ", 20), "a b")
        self.assertEqual(safe_summary("abcdefgh", 5), "abcd…")

    def test_honors_limit_on_every_return_path(self):
        self.assertEqual(safe_summary("abcdefgh", 0), "")
        self.assertEqual(safe_summary("abcdefgh", 1), "…")
        self.assertEqual(safe_summary("api_key=secret-value", 5), "内容可能…")

    def test_preserves_ordinary_secret_related_words(self):
        self.assertEqual(safe_summary("token budget is 100", 300), "token budget is 100")
        self.assertEqual(safe_summary("password reset instructions", 300), "password reset instructions")
        self.assertEqual(safe_summary("release.2026.08", 300), "release.2026.08")
        self.assertEqual(
            safe_summary("survey-service.api.20260810", 300),
            "survey-service.api.20260810",
        )
        long_dotted_value = "surveyey:" + "a" * 4_096 + ".com.resource123"
        self.assertEqual(safe_summary(long_dotted_value, 10_000), long_dotted_value)

    def test_long_hyphenated_non_secret_is_processed_without_pathological_backtracking(self):
        for value in ("a-" * 10_000, "token-" * 5_000):
            with self.subTest(prefix=value[:5]):
                started_at = time.perf_counter()
                safe_summary(value, 300)
                self.assertLess(time.perf_counter() - started_at, 1.0)

    def test_repeated_malformed_jwt_prefixes_do_not_trigger_quadratic_scanning(self):
        value = "eyJ_" * 12_000 + "eyJ.aa.short"
        started_at = time.perf_counter()
        safe_summary(value, 300)
        self.assertLess(time.perf_counter() - started_at, 1.0)

    @patch("codex_notify.redact._is_jose_header", return_value=False)
    def test_overlapping_jose_candidates_are_bounded_and_fail_closed(self, is_header):
        value = f"{'ey' * 2_048}.e30.fakeSignatureValue"

        self.assertEqual(
            safe_summary(value, 300),
            "内容可能包含敏感信息，请回到 Codex 查看。",
        )
        self.assertLessEqual(is_header.call_count, 32)

    @patch("codex_notify.redact.json.loads", side_effect=RecursionError)
    def test_jose_parser_recursion_failure_is_redacted_without_escaping(self, _loads):
        self.assertEqual(
            safe_summary("eyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue", 300),
            "内容可能包含敏感信息，请回到 Codex 查看。",
        )

    def test_oversized_valid_jose_header_fails_closed(self):
        raw_header = b'{"alg":"HS256","x5c":["' + b"a" * 4_000 + b'"]}'
        header = base64.urlsafe_b64encode(raw_header).rstrip(b"=").decode("ascii")

        self.assertGreater(len(header), 4_096)
        self.assertEqual(
            safe_summary(f"{header}.e30.fakeSignatureValue", 300),
            "内容可能包含敏感信息，请回到 Codex 查看。",
        )

    def test_redacts_common_secret_shapes(self):
        private_key_marker = "".join(
            ("-----BEGIN ", "PRIVATE ", "KEY-----")
        )
        pgp_private_key_marker = "".join(
            ("-----BEGIN PGP ", "PRIVATE KEY ", "BLOCK-----")
        )
        stripe_api_key = "".join(("sk_", "live_", "abcdefghijklmnopqrstuvwxyz"))
        stripe_restricted_key = "".join(
            ("rk_", "live_", "abcdefghijklmnopqrstuvwxyz")
        )
        slack_bot_token = "".join(
            ("xoxb-", "123456789012-", "123456789012-", "abcdefghijklmnopqrstuvwxyz")
        )
        stripe_webhook_secret = "".join(
            ("whsec_", "abcdefghijklmnopqrstuvwxyz", "123456")
        )
        cases = [
            "Authorization: Bearer abcdefghijklmnop",
            "api_key=super-secret-value",
            "API key: AIzaFakeCredentialValue",
            "codex run --token fake-cli-token-value",
            "codex run --token=fake-cli-token-value",
            "codex run --api-key=fake-cli-key-value",
            "sk-proj-abcdefghijklmnop",
            stripe_api_key,
            stripe_restricted_key,
            "https://open.feishu.cn/open-apis/bot/v2/hook/secret-hook-id",
            private_key_marker,
            pgp_private_key_marker,
            "AWS_SECRET_ACCESS_KEY=fakeSecret1234567890",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            "github_pat_11AA0_exampletoken1234567890",
            slack_bot_token,
            stripe_webhook_secret,
            "glpat-abcdefghijklmnopqrstuvwxyz123456",
            "AIzaSyD-fakeCredentialValue1234567890",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.fakeSignatureValue",
            "eyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue",
            "debug-eyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue",
            "incident_eyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue",
            "incidenteyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue",
            "ewogICJhbGciOiAiSFMyNTYiCn0.e30.fakeSignatureValue",
            "eyAiYWxnIjoiSFMyNTYifQ.e30.fakeSignatureValue",
            "postgresql://admin:fakePassword@db.example/app",
            "redis://:fakePassword@cache.example/0",
            '{"password":"hunter2"}',
            '{"api_key": "super-secret-value"}',
            r'{\"authorization\":\"Bearer abcdefghijklmnop\"}',
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), "内容可能包含敏感信息，请回到 Codex 查看。")


if __name__ == "__main__":
    unittest.main()
