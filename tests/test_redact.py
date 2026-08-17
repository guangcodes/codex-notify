import base64
import time
import unittest
from unittest.mock import patch

import codex_notify.redact as redact
from codex_notify.redact import safe_summary


_FAKE_MAC_HOME = "/" + "Users/alice"
_FAKE_MAC_EXAMPLE_HOME = "/" + "Users/example"
_FAKE_MAC_SHORT_HOME = "/" + "Users/a"
_FAKE_WINDOWS_POSIX_HOME = "C:/" + "Users/alice"


class SafeSummaryTests(unittest.TestCase):
    def test_compacts_and_truncates(self):
        self.assertEqual(safe_summary(" a\n b ", 20), "a b")
        self.assertEqual(safe_summary("abcdefgh", 5), "abcd…")

    def test_honors_limit_on_every_return_path(self):
        self.assertEqual(safe_summary("abcdefgh", 0), "")
        self.assertEqual(safe_summary("abcdefgh", 1), "…")
        self.assertEqual(safe_summary("api_key=secret-value", 5), "api_…")

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

    def test_redacts_absolute_local_paths_and_markdown_targets(self):
        mac_example_path = "/" + "Users/example/projects/private/report.md"
        mac_private_path = "/" + "Users/alice/private.txt"
        windows_example_path = "C:" + r"\Users\example\private\report.txt"
        cases = (
            mac_example_path,
            "see /private/tmp/internal/output.json for details",
            "/srv/acme/private.txt",
            "/custom/data/report.md",
            "/secret.txt",
            "/[private]/secret.txt",
            "/(private)/secret.txt",
            f"文件在{_FAKE_MAC_HOME}/private.txt",
            "日志在/private/tmp/codex-notify.log",
            f"文件：`{mac_private_path}`",
            "输出：/custom/report.md",
            f"https://example.com，文件：{mac_private_path}",
            f"https://example.com）{mac_private_path}",
            f"https://example.com】{mac_private_path}",
            f"https://example.com—{mac_private_path}",
            f"https://example.com,{mac_private_path}",
            f"https://example.com){mac_private_path}",
            f"cwd:{mac_private_path}",
            f"https://example.com/?file={mac_private_path}",
            "https://example.com/#path=/custom/private.txt",
            f"https://example.com/?target=file://{_FAKE_MAC_HOME}/private.txt",
            "https://example.com/?target=file%3A%2F%2F%2FUsers%2Falice%2Fprivate.txt",
            windows_example_path,
            r"C:\secret.txt",
            r"\\server\share\secret.txt",
            r"https://example.com,C:\Users\alice\secret.txt",
            r"https://example.com)\\server\share\secret.txt",
            r"https://example.com/?file=C:\Users\alice\secret.txt",
            "https://example.com/?file=C:/" + "Users/alice/secret.txt",
            "https://example.com/?file=C%3A%5CUsers%5Calice%5Csecret.txt",
            "https://example.com/?file=C%3A%2FUsers%2Falice%2Fsecret.txt",
            r"https://example.com/?target=C:\Users\alice\secret.txt",
            "https://example.com/?target=C:/" + "Users/alice/secret.txt",
            "https://example.com/?target=C%3A%5CUsers%5Calice%5Csecret.txt",
            "https://example.com/?target=C%3A%2FUsers%2Falice%2Fsecret.txt",
            r"https://example.com/?path=\\server\share\secret.txt",
            "https://example.com/?path=%5C%5Cserver%5Cshare%5Csecret.txt",
            r"https://example.com/?target=\\server\share\secret.txt",
            "https://example.com/?target=%5C%5Cserver%5Cshare%5Csecret.txt",
            f"[report]({mac_example_path})",
            f"[report](file://{mac_example_path})",
        )
        for value in cases:
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)
                self.assertNotEqual(result, "内容可能包含敏感信息，请回到 Codex 查看。")

    def test_preserves_urls_and_relative_paths(self):
        cases = (
            "https://github.com/example/project/pull/1",
            "https://example.com/?next=/dashboard",
            "https://example.com/#/settings/profile",
            "https://example.com/foo(bar)/baz",
            "https://example.com/a(b(c))/docs",
            "https://[::1]/docs",
            "https://example.com/path（文档）",
            "./src/module.py",
            "../docs/readme.md",
            "文档/说明.md",
            "文档/examples/说明.md",
            "fixtures/" + "Users/alice/sample.json",
            "docs/release-notes.md",
            "src/codex_notify/redact.py:131",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), value)

    def test_masks_absolute_paths_after_ascii_punctuation(self):
        cases = (
            f"See...{_FAKE_MAC_HOME}/private.txt",
            f"path-{_FAKE_MAC_HOME}/private.txt",
            "error./tmp/private.txt",
            "output-/custom/private.txt",
        )
        for value in cases:
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)
                self.assertNotIn("private.txt", result)

    def test_preserves_root_relative_http_routes(self):
        for value in (
            "GET /api/v1/users failed",
            "POST /internal/jobs?dry_run=1 returned 409",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), value)

    def test_http_route_does_not_protect_local_path_query_value(self):
        local_path = f"{_FAKE_MAC_HOME}/private.txt"
        value = f"GET /api?file={local_path} failed"

        result = safe_summary(value, 300)

        self.assertEqual(result, "GET /api?file=[本地路径已打码] failed")
        self.assertNotIn(local_path, result)

    def test_http_method_does_not_protect_known_local_root(self):
        local_path = "/" + "Users/alice/private.txt"
        result = safe_summary("GET " + local_path, 300)

        self.assertEqual(result, "GET [本地路径已打码]")

    def test_lowercase_prose_verb_does_not_protect_custom_local_path(self):
        value = "get /workspace/private/report.txt"

        result = safe_summary(value, 300)

        self.assertEqual(result, "get [本地路径已打码]")
        self.assertNotIn("/workspace/private/report.txt", result)

    def test_masks_known_local_roots_in_any_url_parameter(self):
        local_path = "/" + "Users/alice/private.txt"
        cases = (
            "https://example.com/?redirect=" + local_path,
            "https://example.com/?redirect=%2FUsers%2Falice%2Fprivate.txt",
        )
        for value in cases:
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertEqual(
                    result,
                    "https://example.com/?redirect=[本地路径已打码]",
                )

    def test_masks_complete_spaced_path_in_url_parameter(self):
        local_path = _FAKE_MAC_HOME + "/Top Secret/report.md"
        value = "https://example.com/?file=" + local_path

        result = safe_summary(value, 300)

        self.assertEqual(result, "https://example.com/?file=[本地路径已打码]")
        self.assertNotIn("Secret/report.md", result)

    def test_long_hyphenated_non_secret_is_processed_without_pathological_backtracking(self):
        for value in ("a-" * 10_000, "token-" * 5_000):
            with self.subTest(prefix=value[:5]):
                started_at = time.perf_counter()
                safe_summary(value, 300)
                self.assertLess(time.perf_counter() - started_at, 1.0)

    def test_repeated_slash_candidates_do_not_trigger_quadratic_scanning(self):
        value = ("(/a" * 6_000)[:16_000]
        started_at = time.perf_counter()

        result = safe_summary(value, 300)

        self.assertLess(time.perf_counter() - started_at, 1.0)
        self.assertIn("[本地路径已打码]", result)

    def test_candidates_inside_an_accepted_path_span_are_not_rescanned(self):
        value = ("/a.txt " * 3_000)[:16_000]

        with patch(
            "codex_notify.redact._scan_local_path_end",
            wraps=redact._scan_local_path_end,
        ) as scan:
            result = safe_summary(value, 300)

        self.assertIn("[本地路径已打码]", result)
        self.assertLessEqual(scan.call_count, 10)

    def test_explicit_relative_path_candidates_are_rejected_before_scanning(self):
        for fragment in ("文档/a ", "./a ", "../a "):
            value = (fragment * 3_000)[:16_000]
            with self.subTest(fragment=fragment), patch(
                "codex_notify.redact._scan_local_path_end",
                wraps=redact._scan_local_path_end,
            ) as scan:
                result = safe_summary(value, 300)

            self.assertNotIn("[本地路径已打码]", result)
            self.assertEqual(scan.call_count, 0)

    def test_concatenated_urls_are_processed_without_quadratic_retries(self):
        value = "".join(f"https://example.com/{index}" for index in range(5_000))
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
            "[敏感信息已打码]",
        )
        self.assertLessEqual(is_header.call_count, 32)

    @patch("codex_notify.redact.json.loads", side_effect=RecursionError)
    def test_jose_parser_recursion_failure_is_redacted_without_escaping(self, _loads):
        self.assertEqual(
            safe_summary("eyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue", 300),
            "[敏感信息已打码]",
        )

    def test_oversized_valid_jose_header_fails_closed(self):
        raw_header = b'{"alg":"HS256","x5c":["' + b"a" * 4_000 + b'"]}'
        header = base64.urlsafe_b64encode(raw_header).rstrip(b"=").decode("ascii")

        self.assertGreater(len(header), 4_096)
        self.assertEqual(
            safe_summary(f"{header}.e30.fakeSignatureValue", 300),
            "[敏感信息已打码]",
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
                result = safe_summary(value, 300)
                self.assertIn("[敏感信息已打码]", result)
                self.assertNotEqual(result, "内容可能包含敏感信息，请回到 Codex 查看。")

    def test_masks_only_sensitive_fragments_and_keeps_task_context(self):
        value = (
            f"任务 codex-notify 已完成；输出 {_FAKE_MAC_HOME}/private/report.md；"
            'api_key="super-secret-value"；请检查结果'
        )
        result = safe_summary(value, 300)

        self.assertIn("任务 codex-notify 已完成", result)
        self.assertIn("请检查结果", result)
        self.assertIn("[本地路径已打码]", result)
        self.assertIn("[敏感信息已打码]", result)
        self.assertNotIn(_FAKE_MAC_HOME, result)
        self.assertNotIn("super-secret-value", result)

    def test_preserves_codex_slash_commands(self):
        for value in (
            "/review",
            "/plan",
            "/goal inspect notifications",
            "/goal inspect release 1.2",
            "/hooks",
            "/status",
            "/model gpt-5.6-sol",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), value)

    def test_preserves_codex_slash_commands_mentioned_in_prose(self):
        for value in (
            "Please run /review current changes",
            "Please run command /review current changes",
            "After fixing, use /status.",
            "请执行 /plan 继续处理",
            "请执行 Codex 的 /plan 继续处理",
            "Use `/hooks` and confirm trust",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), value)

    def test_slash_command_protection_does_not_hide_following_local_path(self):
        value = f"Please run /review {_FAKE_MAC_HOME}/private/report.md"

        result = safe_summary(value, 300)

        self.assertEqual(result, "Please run /review [本地路径已打码]")
        self.assertNotIn(_FAKE_MAC_HOME, result)

    def test_quoted_slash_name_requires_command_context(self):
        self.assertEqual(
            safe_summary('cannot open "/review"', 300),
            'cannot open "[本地路径已打码]"',
        )

    def test_slash_command_name_does_not_protect_file_extension(self):
        for value in ("/review.md", "/status.log"):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), "[本地路径已打码]")

    def test_slash_command_protection_does_not_exempt_overlapping_path(self):
        for value in ("/review data/private/report.md", "/review data.md"):
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertEqual(result, "/review [本地路径已打码]")
                self.assertNotIn("data", result)

    def test_masks_single_segment_absolute_directory(self):
        for value in (
            "output is in /workspace",
            "文件位于 /秘密",
            "output saved in /review",
            "output saved in /Userspace",
        ):
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)

    def test_masks_custom_absolute_path_after_cjk_context(self):
        self.assertEqual(
            safe_summary("文件在/custom/data/report.md", 300),
            "文件在[本地路径已打码]",
        )

    def test_masks_shallow_custom_path_after_arbitrary_cjk_text(self):
        self.assertEqual(
            safe_summary("结果在/custom/report.md", 300),
            "结果在[本地路径已打码]",
        )

    def test_masks_shallow_absolute_paths_adjacent_to_cjk_text(self):
        for value in (
            f"请检查{_FAKE_MAC_HOME}",
            "错误在/secret.txt",
        ):
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)
                self.assertNotIn(value[value.index("/"):], result)

    def test_masks_absolute_paths_after_arbitrary_cjk_text(self):
        prefixes = (
            "输出",
            "见",
            "请看",
            "任务",
            "任意文本",
            "删除项目",
            "请查看文档",
        )
        paths = (
            "/custom/private.txt",
            r"C:\custom\private.txt",
            r"\\server\share\private.txt",
        )
        for prefix in prefixes:
            for path in paths:
                with self.subTest(prefix=prefix, path=path):
                    self.assertEqual(
                        safe_summary(prefix + path, 300),
                        prefix + "[本地路径已打码]",
                    )

    def test_preserves_explicit_cjk_relative_path_roots(self):
        for value in (
            "文档/说明.md",
            "文档/examples/说明.md",
            "项目/说明.md",
            "源码/module.py",
            "测试/fixtures.json",
            "请查看 文档/说明.md",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), value)

    def test_masks_shallow_custom_paths_after_cjk_path_actions(self):
        for value in (
            "读取/custom/file",
            "打开/custom/file.txt",
            "请检查/custom/file",
        ):
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)
                self.assertNotIn("/custom/file", result)

    def test_masks_sqlite_local_uri(self):
        value = f"database=sqlite:///{_FAKE_MAC_HOME}/private.db"

        result = safe_summary(value, 300)

        self.assertEqual(result, "database=[本地路径已打码]")
        self.assertNotIn(_FAKE_MAC_HOME, result)

    def test_masks_editor_local_path_uris(self):
        local_path = _FAKE_MAC_HOME + "/private.py"
        for scheme in ("vscode", "vscode-insiders", "cursor", "windsurf", "zed"):
            value = f"open {scheme}://file{local_path}"
            with self.subTest(scheme=scheme):
                result = safe_summary(value, 300)
                self.assertEqual(result, "open [本地路径已打码]")
                self.assertNotIn(local_path, result)
        self.assertEqual(
            safe_summary("请检查/custom/data/report.md", 300),
            "请检查[本地路径已打码]",
        )

    def test_masks_encoded_local_path_parameters_in_local_uris(self):
        cases = (
            (
                "file:///tmp/a?path=%2FUsers%2Falice%2Fsecret.txt",
                "[本地路径已打码]?path=[本地路径已打码]",
            ),
            (
                "vscode://file/tmp/a#cwd=%2FUsers%2Falice%2Fprivate",
                "[本地路径已打码]#cwd=[本地路径已打码]",
            ),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertEqual(result, expected)
                self.assertNotIn("%2FUsers%2Falice", result)

    def test_masks_standalone_percent_encoded_local_paths(self):
        cases = (
            "file%3A%2F%2F%2FUsers%2Falice%2Fsecret.txt",
            "C%3A%5CUsers%5Calice%5Csecret.txt",
            "C%3A%2FUsers%2Falice%2Fsecret.txt",
            "%5C%5Cserver%5Cshare%5Csecret.txt",
            "%2FUsers%2Falice%2Fsecret.txt",
        )
        for path in cases:
            with self.subTest(path=path):
                self.assertEqual(
                    safe_summary(f"saved {path} then continue", 300),
                    "saved [本地路径已打码] then continue",
                )

    def test_preserves_non_path_percent_encoded_values(self):
        for value in (
            "https%3A%2F%2Fexample.com%2Fdocs",
            "%2Freview",
            "prefixfile%3A%2F%2F%2FUsers%2Falice%2Fsecret.txt",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), value)

    def test_masks_local_path_in_unlisted_non_web_uri_scheme(self):
        cases = (
            (
                f"open vscode-remote://ssh-remote+host{_FAKE_MAC_HOME}/private.py",
                "open vscode-remote://ssh-remote+host[本地路径已打码]",
            ),
            (
                f"vscode-remote://host/{_FAKE_WINDOWS_POSIX_HOME}/private.py",
                "vscode-remote://host/[本地路径已打码]",
            ),
            (
                r"custom://host/\\server\share\private.py",
                "custom://host/[本地路径已打码]",
            ),
            (
                "custom://host/C%3A%2FUsers%2Falice%2Fprivate.py",
                "custom://host/[本地路径已打码]",
            ),
            (
                "custom://host/%5C%5Cserver%5Cshare%5Cprivate.py",
                "custom://host/[本地路径已打码]",
            ),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), expected)

    def test_masks_complete_spaced_editor_local_path_uri(self):
        value = f"vscode://file{_FAKE_MAC_HOME}/Client Project/report.md"

        result = safe_summary(value, 300)

        self.assertEqual(result, "[本地路径已打码]")
        self.assertNotIn("Project/report.md", result)

    def test_preserves_markdown_delimiters_around_masked_paths(self):
        self.assertEqual(
            safe_summary(f"[report]({_FAKE_MAC_EXAMPLE_HOME}/report.md)", 300),
            "[report]([本地路径已打码])",
        )
        self.assertEqual(
            safe_summary(
                "[report](https://example.com/?file=/" + "Users/a/report.md)", 300
            ),
            "[report](https://example.com/?file=[本地路径已打码])",
        )

    def test_adjacent_markdown_web_and_local_links_have_separate_spans(self):
        self.assertEqual(
            safe_summary(
                f"[web](https://example.com)[local]({_FAKE_MAC_HOME}/private.txt)",
                300,
            ),
            "[web](https://example.com)[local]([本地路径已打码])",
        )

    def test_masks_complete_private_key_block_and_preserves_context(self):
        begin = "".join(("-----BEGIN ", "PRIVATE ", "KEY-----"))
        end = "".join(("-----END ", "PRIVATE ", "KEY-----"))
        value = f"部署任务失败：{begin}\nsecret-body-material\n{end}；请轮换密钥"
        result = safe_summary(value, 300)

        self.assertEqual(result, "部署任务失败：[敏感信息已打码]；请轮换密钥")
        self.assertNotIn("secret-body-material", result)

    def test_masks_truncated_private_key_from_begin_marker_to_end(self):
        begin = "".join(("-----BEGIN ", "PRIVATE ", "KEY-----"))
        value = f"部署任务失败：{begin} secret-body-material"
        result = safe_summary(value, 300)

        self.assertEqual(result, "部署任务失败：[敏感信息已打码]")
        self.assertNotIn("secret-body-material", result)

    def test_masks_truncated_private_key_after_complete_block(self):
        begin = "".join(("-----BEGIN ", "PRIVATE ", "KEY-----"))
        end = "".join(("-----END ", "PRIVATE ", "KEY-----"))
        value = f"第一段 {begin} body-one {end}；第二段 {begin} body-two"

        result = safe_summary(value, 500)

        self.assertEqual(result, "第一段 [敏感信息已打码]；第二段 [敏感信息已打码]")
        self.assertNotIn("body-one", result)
        self.assertNotIn("body-two", result)

    def test_masks_complete_quoted_secret_value(self):
        self.assertEqual(
            safe_summary('配置失败：{"password":"correct horse battery"}；请检查', 300),
            '配置失败：{"password":"[敏感信息已打码]"}；请检查',
        )

    def test_masks_triple_quoted_assignment_value(self):
        self.assertEqual(
            safe_summary('password = """super-secret"""', 300),
            'password = """[敏感信息已打码]"""',
        )

    def test_escaped_triple_quote_does_not_end_secret(self):
        value = 'password="""abc\\"""def""" then continue'

        self.assertEqual(
            safe_summary(value, 300),
            'password="""[敏感信息已打码]""" then continue',
        )

    def test_quoted_assignment_keeps_context_after_closing_quote(self):
        self.assertEqual(
            safe_summary('password = "secret" then continue', 300),
            'password = "[敏感信息已打码]" then continue',
        )

    def test_masks_suffix_concatenated_to_quoted_assignment_value(self):
        for value in (
            'password="abc"def then continue',
            'password="abc"@example then continue',
            'password="abc"%2Fprivate then continue',
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    safe_summary(value, 300),
                    "password=[敏感信息已打码] then continue",
                )

    def test_masks_adjacent_quoted_assignment_fragments(self):
        for value in (
            'password="abc"\'def\' then continue',
            'password="abc"\'def\'"ghi" then continue',
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    safe_summary(value, 300),
                    "password=[敏感信息已打码] then continue",
                )

    def test_assignment_shaped_text_inside_quoted_secret_keeps_context(self):
        self.assertEqual(
            safe_summary('password="abc token=x" then continue', 300),
            'password="[敏感信息已打码]" then continue',
        )

    def test_masks_quoted_secret_with_escaped_quote(self):
        value = '{"password":"abc\\"def"}；请检查'
        result = safe_summary(value, 300)

        self.assertEqual(result, '{"password":"[敏感信息已打码]"}；请检查')
        self.assertNotIn("abc", result)
        self.assertNotIn("def", result)

    def test_masks_escaped_json_quoted_secret(self):
        value = r'{\"password\":\"hunter2\"}；请检查'
        result = safe_summary(value, 300)

        self.assertEqual(result, r'{\"password\":\"[敏感信息已打码]\"}；请检查')
        self.assertNotIn("hunter2", result)

    def test_masks_unterminated_quoted_secret_to_natural_boundary(self):
        value = '配置失败：password="correct horse；请检查任务'
        result = safe_summary(value, 300)

        self.assertEqual(
            result,
            '配置失败：password="[敏感信息已打码]；请检查任务',
        )
        self.assertNotIn("correct horse", result)

    def test_unterminated_quoted_assignment_keeps_ascii_punctuation_secret(self):
        self.assertEqual(
            safe_summary('password="abc,def;ghi', 300),
            'password="[敏感信息已打码]',
        )

    def test_masks_unterminated_quoted_cli_secret_to_natural_boundary(self):
        self.assertEqual(
            safe_summary('--password "correct horse；请检查任务', 300),
            '--password [敏感信息已打码]；请检查任务',
        )

    def test_unterminated_quoted_cli_secret_keeps_ascii_punctuation_secret(self):
        self.assertEqual(
            safe_summary('--password "abc,def;ghi', 300),
            '--password [敏感信息已打码]',
        )

    def test_masks_escaped_json_secret_with_escaped_quote_and_keeps_context(self):
        value = r'{\"password\":\"abc\\\"def\"}；请检查'

        result = safe_summary(value, 300)

        self.assertEqual(result, r'{\"password\":\"[敏感信息已打码]\"}；请检查')
        self.assertNotIn("abc", result)
        self.assertNotIn("def", result)

    def test_escaped_quote_before_whitespace_does_not_end_secret(self):
        value = r'password=\"abc\\\" def\" then continue'

        self.assertEqual(
            safe_summary(value, 300),
            r'password=\"[敏感信息已打码]\" then continue',
        )

    def test_escaped_quote_before_punctuation_does_not_end_secret(self):
        value = r'{\"password\":\"abc\\\",def\"}'

        result = safe_summary(value, 300)

        self.assertEqual(result, r'{\"password\":\"[敏感信息已打码]\"}')
        self.assertNotIn("abc", result)
        self.assertNotIn("def", result)

    def test_escaped_json_secret_stops_before_next_property(self):
        value = r'{\"password\":\"secret\",\"status\":\"failed\"}'

        result = safe_summary(value, 300)

        self.assertEqual(
            result,
            r'{\"password\":\"[敏感信息已打码]\",\"status\":\"failed\"}',
        )
        self.assertNotIn("secret", result)

    def test_escaped_quoted_assignment_preserves_delimited_context(self):
        for delimiter in ",;:.!?，；。：！？":
            value = f'password=\\"secret\\"{delimiter} retry later'
            with self.subTest(delimiter=delimiter):
                self.assertEqual(
                    safe_summary(value, 300),
                    f'password=\\"[敏感信息已打码]\\"{delimiter} retry later',
                )

    def test_masks_escaped_json_secret_ending_in_backslash(self):
        value = r'{\"password\":\"abc,def' + "\\" * 3 + '"}；请检查'

        result = safe_summary(value, 300)

        self.assertEqual(result, r'{\"password\":\"[敏感信息已打码]\"}；请检查')
        self.assertNotIn("def", result)

    def test_masks_complete_unquoted_multi_word_secret(self):
        value = "任务失败：password: correct horse battery；请检查配置"
        result = safe_summary(value, 300)

        self.assertEqual(
            result,
            "任务失败：password: [敏感信息已打码]",
        )
        self.assertNotIn("horse battery", result)

    def test_unquoted_assignment_masks_delimiter_bearing_secret_suffix(self):
        self.assertEqual(
            safe_summary("password=abc,def", 300),
            "password=[敏感信息已打码]",
        )

    def test_masks_single_dash_sensitive_assignments(self):
        self.assertEqual(
            safe_summary("-token=private-value", 300),
            "-token=[敏感信息已打码]",
        )
        self.assertEqual(
            safe_summary('-password="abc def" then continue', 300),
            '-password="[敏感信息已打码]" then continue',
        )

    def test_unquoted_secret_after_ordinary_assignment_is_still_masked(self):
        value = "status=ok api_key=supersecretvalue"

        result = safe_summary(value, 300)

        self.assertEqual(result, "status=ok api_key=[敏感信息已打码]")
        self.assertNotIn("supersecretvalue", result)

    def test_unquoted_assignment_fails_closed_at_ambiguous_sentence_boundary(self):
        self.assertEqual(
            safe_summary("password=secret. Retry later", 300),
            "password=[敏感信息已打码]",
        )

    def test_unquoted_assignment_keeps_lowercase_punctuation_in_secret(self):
        self.assertEqual(
            safe_summary("password=correct. horse battery", 300),
            "password=[敏感信息已打码]",
        )

    def test_unquoted_assignment_keeps_uppercase_punctuation_in_secret(self):
        self.assertEqual(
            safe_summary("password=correct. Horse battery", 300),
            "password=[敏感信息已打码]",
        )

    def test_masks_file_uri_with_spaces_without_losing_following_context(self):
        value = f"读取 file://{_FAKE_MAC_HOME}/Quarterly Plan/report.md；生成报告"
        self.assertEqual(
            safe_summary(value, 300),
            "读取 [本地路径已打码]；生成报告",
        )

    def test_masks_authenticated_url_userinfo_and_keeps_host(self):
        result = safe_summary(
            "请求 https://api-secret:unused@example.com/v1 失败", 300
        )
        self.assertEqual(
            result,
            "请求 https://[敏感信息已打码]@example.com/v1 失败",
        )
        self.assertNotIn("api-secret", result)
        self.assertNotIn("unused", result)

    def test_masks_authenticated_url_password_containing_at_sign(self):
        result = safe_summary("https://alice:p@ss@example.com/path", 300)

        self.assertEqual(
            result,
            "https://[敏感信息已打码]@example.com/path",
        )
        self.assertNotIn("p@ss", result)

    def test_authenticated_url_stops_userinfo_before_query_at_sign(self):
        value = "https://alice:pw@example.com?email=a@b"

        result = safe_summary(value, 300)

        self.assertEqual(
            result,
            "https://[敏感信息已打码]@example.com?email=a@b",
        )
        self.assertNotIn("alice:pw", result)

    def test_masks_absolute_paths_with_spaces(self):
        cases = (
            f"{_FAKE_MAC_HOME}/Client Project/report.md",
            "文件在/custom/Client Project/report.md；请检查",
            r"C:\Users\alice\Client Project\report.md",
        )
        for value in cases:
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)
                self.assertNotIn("Project", result)

    def test_masks_punctuation_inside_absolute_path_components(self):
        cases = (
            f"{_FAKE_MAC_HOME}/Client,Secret/report.md",
            f"{_FAKE_MAC_HOME}/foo;bar.txt",
            r"C:\Users\alice\Client,Secret\report.md",
            r"C:\Users\alice\foo;bar.txt",
            r"\\server\share\Client,Secret\report.md",
            r"\\server\share\foo;bar.txt",
        )
        for path in cases:
            with self.subTest(path=path):
                result = safe_summary(f"saved {path} then continue", 300)
                self.assertEqual(result, "saved [本地路径已打码] then continue")
                self.assertNotIn("Secret", result)
                self.assertNotIn("bar.txt", result)

    def test_masks_cjk_punctuation_inside_absolute_path_components(self):
        for punctuation in "，。；：、！？】—":
            path = f"{_FAKE_MAC_HOME}/客户{punctuation}秘密/report.md"
            with self.subTest(punctuation=punctuation):
                self.assertEqual(
                    safe_summary(f"saved {path} then continue", 300),
                    "saved [本地路径已打码] then continue",
                )

    def test_masks_quotes_inside_continuing_posix_path_components(self):
        for quote in "'\"`":
            path = f"{_FAKE_MAC_HOME}/O{quote}Brien/private.txt"
            with self.subTest(quote=quote):
                self.assertEqual(
                    safe_summary(f"saved {path} then continue", 300),
                    "saved [本地路径已打码] then continue",
                )

    def test_masks_embedded_quote_matching_the_outer_path_delimiter(self):
        self.assertEqual(
            safe_summary(
                f"saved '{_FAKE_MAC_HOME}/O'Brien/private.txt' then continue",
                300,
            ),
            "saved '[本地路径已打码]' then continue",
        )

    def test_masks_boundary_punctuation_inside_terminal_path_component(self):
        for punctuation in "'\"`)]}，。；：、！？】—":
            path = f"{_FAKE_MAC_HOME}/Client{punctuation}Secret.txt"
            with self.subTest(punctuation=punctuation):
                self.assertEqual(
                    safe_summary(f"saved {path} then continue", 300),
                    "saved [本地路径已打码] then continue",
                )

    def test_masks_boundary_punctuation_inside_extensionless_terminal_component(self):
        for punctuation in "'\"`)]}，。；：、！？】—":
            path = f"{_FAKE_MAC_HOME}/Client{punctuation}Secret"
            with self.subTest(punctuation=punctuation):
                self.assertEqual(safe_summary(path, 300), "[本地路径已打码]")

    def test_extensionless_quoted_path_preserves_following_context(self):
        self.assertEqual(
            safe_summary(
                f"saved '{_FAKE_MAC_HOME}/客户。秘密' then continue", 300
            ),
            "saved '[本地路径已打码]' then continue",
        )

    def test_masks_unmatched_closers_inside_continuing_posix_paths(self):
        for closer in ")]}":
            value = f"/tmp/Client{closer}Secret/report.md"
            with self.subTest(closer=closer):
                self.assertEqual(safe_summary(value, 300), "[本地路径已打码]")

    def test_masks_punctuation_inside_structured_local_path_values(self):
        cases = (
            f"file://{_FAKE_MAC_HOME}/Client,Secret/report.md then continue",
            f"https://example.com/?file={_FAKE_MAC_HOME}/foo;bar.txt then continue",
            f"GET /api?file={_FAKE_MAC_HOME}/Client,Secret/report.md failed",
        )
        expected = (
            "[本地路径已打码] then continue",
            "https://example.com/?file=[本地路径已打码] then continue",
            "GET /api?file=[本地路径已打码] failed",
        )
        for value, rendered in zip(cases, expected, strict=True):
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertEqual(result, rendered)
                self.assertNotIn("Secret", result)
                self.assertNotIn("bar.txt", result)

    def test_structured_path_spans_preserve_following_url_context(self):
        cases = (
            (
                f"https://x.test/?path={_FAKE_MAC_SHORT_HOME}/private.txt&keep=1",
                "https://x.test/?path=[本地路径已打码]&keep=1",
            ),
            (
                "https://x.test/?path=C:\\Users\\a\\private.txt#section",
                "https://x.test/?path=[本地路径已打码]#section",
            ),
            (
                "https://x.test/?path=\\\\server\\share\\private.txt&keep=1",
                "https://x.test/?path=[本地路径已打码]&keep=1",
            ),
            (
                f"https://x.test/?path={_FAKE_MAC_SHORT_HOME}/private.txt&cwd=/tmp/work",
                "https://x.test/?path=[本地路径已打码]&cwd=[本地路径已打码]",
            ),
            (
                f"GET /api?file={_FAKE_MAC_SHORT_HOME}/private.txt&keep=1 failed",
                "GET /api?file=[本地路径已打码]&keep=1 failed",
            ),
            (
                "POST /api?cwd=C:\\Users\\a\\work#trace returned 200",
                "POST /api?cwd=[本地路径已打码]#trace returned 200",
            ),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 500), expected)

    def test_nested_local_uri_owns_only_its_outer_parameter_value(self):
        cases = (
            (
                f"https://example.com/?target=file://{_FAKE_MAC_SHORT_HOME}/private.txt&keep=1",
                "https://example.com/?target=[本地路径已打码]&keep=1",
            ),
            (
                f"https://example.com/#target=file://{_FAKE_MAC_SHORT_HOME}/private.txt&keep=1",
                "https://example.com/#target=[本地路径已打码]&keep=1",
            ),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 500), expected)

    def test_extensionless_spaced_path_fails_closed_without_syntax_boundary(self):
        cases = (
            "saved /tmp/Top Secret then continue",
            r"saved C:\tmp\Top Secret then continue",
            r"saved \\server\share\Top Secret then continue",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    safe_summary(value, 300),
                    "saved [本地路径已打码]",
                )

    def test_connector_like_words_inside_spaced_paths_remain_masked(self):
        for word in (
            "status",
            "then",
            "failed",
            "completed",
            "generated",
            "returned",
        ):
            value = f"saved {_FAKE_MAC_HOME}/report {word} final.md then continue"
            with self.subTest(word=word):
                self.assertEqual(
                    safe_summary(value, 300),
                    "saved [本地路径已打码] then continue",
                )

    def test_extension_like_component_before_path_continuation_remains_masked(self):
        for value in (
            f"{_FAKE_MAC_SHORT_HOME}/report.md private/secret",
            r"C:\Users\a\report.md private\secret",
            r"\\server\share\report.md private\secret",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), "[本地路径已打码]")

    def test_windows_terminal_separator_preserves_closing_delimiter_and_context(self):
        cases = (
            (
                'see "C:\\Users\\alice\\Private\\" now',
                'see "[本地路径已打码]" now',
            ),
            (
                r"see (C:\Users\alice\Private\) now",
                "see ([本地路径已打码]) now",
            ),
            (
                'see "\\\\server\\share\\Private\\" now',
                'see "[本地路径已打码]" now',
            ),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), expected)

    def test_posix_escaped_space_remains_inside_path(self):
        self.assertEqual(
            safe_summary(
                f"saved {_FAKE_MAC_SHORT_HOME}/Quarterly\\ Report.md then continue",
                300,
            ),
            "saved [本地路径已打码] then continue",
        )

    def test_path_scanner_preserves_punctuation_in_following_prose(self):
        cases = (
            ("saved /tmp/report.txt, then continue", "saved [本地路径已打码], then continue"),
            ("saved /tmp/report.txt; status is green", "saved [本地路径已打码]; status is green"),
            ("saved /tmp/report.txt. Upload complete.", "saved [本地路径已打码]. Upload complete."),
            ("saved /tmp/report.txt: upload complete", "saved [本地路径已打码]: upload complete"),
            ("saved /tmp/report.txt! Upload complete.", "saved [本地路径已打码]! Upload complete."),
            ("saved /tmp/report.txt? Check again.", "saved [本地路径已打码]? Check again."),
            (r"saved C:\tmp\report.txt, then continue", "saved [本地路径已打码], then continue"),
            (r"saved \\server\share\report.txt; status is green", "saved [本地路径已打码]; status is green"),
            (r"saved C:\tmp\report.txt. Upload complete.", "saved [本地路径已打码]. Upload complete."),
            (r"saved \\server\share\report.txt: upload complete", "saved [本地路径已打码]: upload complete"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), expected)

    def test_punctuation_and_space_inside_path_component_remain_masked(self):
        for punctuation in (".", ",", ";", ":", "!", "?"):
            value = (
                f"saved {_FAKE_MAC_SHORT_HOME}/Client{punctuation} Secret/report.md"
            )
            with self.subTest(punctuation=punctuation):
                self.assertEqual(safe_summary(value, 300), "saved [本地路径已打码]")

    def test_overlong_path_candidate_fails_closed_without_suffix_leak(self):
        private_suffix = "private-tail/report.md"
        value = "/tmp/" + "a" * 2_100 + "/" + private_suffix

        result = safe_summary(value, 10_000)

        self.assertEqual(result, "[本地路径已打码]")
        self.assertNotIn(private_suffix, result)

    def test_masks_spaces_in_final_filename_and_preserves_following_text(self):
        cases = (
            f"{_FAKE_MAC_HOME}/Quarterly Report.md 后续说明",
            "文件在/custom/Quarterly Report.md 后续说明",
            r"C:\Users\alice\Quarterly Report.md 后续说明",
            f"file://{_FAKE_MAC_HOME}/Quarterly Report.md 后续说明",
        )
        for value in cases:
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)
                self.assertIn("后续说明", result)
                self.assertNotIn("Report.md", result)

    def test_masks_complete_spaced_filename_after_extension_like_token(self):
        value = _FAKE_MAC_HOME + "/client.v2 report.md"

        result = safe_summary(value, 300)

        self.assertEqual(result, "[本地路径已打码]")
        self.assertNotIn("report.md", result)

    def test_masks_ascii_path_suffix_after_extension_like_component(self):
        value = _FAKE_MAC_HOME + "/client.v2 private"

        result = safe_summary(value, 300)

        self.assertEqual(result, "[本地路径已打码]")
        self.assertNotIn("private", result)

    def test_spaced_path_stops_before_closing_delimiter_and_context(self):
        cases = (
            ('see "/' + 'Users/a/Quarterly Report.md" completed', 'see "[本地路径已打码]" completed'),
            ("see (/" + "Users/a/Quarterly Report.md) completed", "see ([本地路径已打码]) completed"),
            ("see `/" + "Users/a/Quarterly Report.md` completed", "see `[本地路径已打码]` completed"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), expected)

    def test_masks_paths_ending_in_spaced_directory_to_natural_boundary(self):
        cases = (
            f"{_FAKE_MAC_HOME}/Top Secret；后续说明",
            "文件在/custom/Top Secret；后续说明",
            r"C:\Users\alice\Top Secret；后续说明",
            f"file://{_FAKE_MAC_HOME}/Top Secret；后续说明",
        )
        for value in cases:
            with self.subTest(value=value):
                result = safe_summary(value, 300)
                self.assertIn("[本地路径已打码]", result)
                self.assertIn("后续说明", result)
                self.assertNotIn("Top Secret", result)

    def test_masks_root_level_spaced_absolute_path_as_one_span(self):
        self.assertEqual(
            safe_summary("/Top Secret", 300),
            "[本地路径已打码]",
        )

    def test_does_not_treat_spaced_fraction_as_absolute_path(self):
        self.assertEqual(
            safe_summary("passed 3 / 4 checks", 300),
            "passed 3 / 4 checks",
        )

    def test_masks_complete_quoted_cli_secret_value(self):
        self.assertEqual(
            safe_summary('--password "correct horse battery"', 300),
            '--password [敏感信息已打码]',
        )

    def test_masks_shell_escaped_cli_secret_argument(self):
        self.assertEqual(
            safe_summary(r"--password correct\ horse then continue", 300),
            "--password [敏感信息已打码] then continue",
        )

    def test_masks_punctuation_and_concatenated_cli_secret_fragments(self):
        for value, expected in (
            ("--password=abc,def then", "--password=[敏感信息已打码] then"),
            ('--password "abc"def then', "--password [敏感信息已打码] then"),
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_summary(value, 300), expected)

    def test_masks_jwt_after_dotted_label_without_hiding_label(self):
        self.assertEqual(
            safe_summary(
                "debug.eyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue",
                300,
            ),
            "debug.[敏感信息已打码]",
        )

    def test_does_not_treat_non_dot_separated_segments_as_jwt(self):
        value = "release.2026.08 path/eyJhbGciOiJIUzI1NiJ9/e30/fakeSignatureValue"
        self.assertEqual(safe_summary(value, 300), value)

    def test_oversized_input_is_bounded_before_whitespace_compaction(self):
        value = "ordinary text " * 100_000
        started_at = time.perf_counter()
        result = safe_summary(value, 300)

        self.assertLess(time.perf_counter() - started_at, 1.0)
        self.assertLessEqual(len(result), 300)
        self.assertTrue(result.endswith("…"))

    def test_whitespace_prefix_does_not_consume_compact_scan_budget(self):
        self.assertEqual(safe_summary(" " * 17_000 + "error", 300), "error")

    def test_oversized_redacted_input_marks_truncation_at_exact_limit(self):
        mask_length = len("[敏感信息已打码]")
        value = (
            "a" * (300 - mask_length)
            + "".join(("-----BEGIN ", "PRIVATE ", "KEY-----"))
            + "secret " * 10_000
        )

        result = safe_summary(value, 300)

        self.assertEqual(len(result), 300)
        self.assertTrue(result.endswith("…"))

    def test_redacts_secret_beyond_compact_scan_boundary_within_raw_cap(self):
        cases = (
            " " * 16_380 + "sk-proj-" + "a" * 40,
            " " * 16_380
            + "eyJhbGciOiJIUzI1NiJ9.e30.fakeSignatureValue",
        )
        for value in cases:
            with self.subTest(suffix=value[-20:]):
                self.assertEqual(safe_summary(value, 300), "[敏感信息已打码]")


if __name__ == "__main__":
    unittest.main()
