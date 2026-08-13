import base64
import hashlib
import hmac
import json
import unittest
import urllib.error
from unittest.mock import patch

from codex_notify.feishu import (
    DeliveryError,
    FeishuClient,
    make_signature,
    validate_webhook_url,
)
from codex_notify.keychain import FeishuCredentials


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return b'{"code": 0, "msg": "success"}'


class _ThrottledResponse(_Response):
    def read(self, limit):
        return b'{"code": 9499, "msg": "too many requests"}'


class _NonObjectResponse(_Response):
    def read(self, limit):
        return b'[]'


class FeishuTests(unittest.TestCase):
    def test_signature_uses_feishu_algorithm(self):
        timestamp = 1_599_360_473
        secret = "test-secret"
        expected = base64.b64encode(
            hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
        ).decode()
        self.assertEqual(make_signature(timestamp, secret), expected)

    def test_rejects_non_feishu_webhook(self):
        with self.assertRaises(ValueError):
            validate_webhook_url("https://example.com/open-apis/bot/v2/hook/leak")
        with self.assertRaises(ValueError):
            validate_webhook_url("https://evil@open.feishu.cn/open-apis/bot/v2/hook/leak")
        with self.assertRaises(ValueError):
            validate_webhook_url("https://open.feishu.cn/open-apis/bot/v2/hook/")

    @patch("codex_notify.feishu.urllib.request.urlopen", return_value=_Response())
    def test_sends_signed_text_payload(self, urlopen):
        credentials = FeishuCredentials(
            "https://open.feishu.cn/open-apis/bot/v2/hook/example", "secret"
        )
        FeishuClient(credentials).send_text("hello", timestamp=123)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["content"]["text"], "hello")
        self.assertEqual(payload["timestamp"], "123")
        self.assertTrue(payload["sign"])

    @patch(
        "codex_notify.feishu.urllib.request.urlopen",
        return_value=_ThrottledResponse(),
    )
    def test_body_level_throttling_is_retryable(self, _urlopen):
        credentials = FeishuCredentials(
            "https://open.feishu.cn/open-apis/bot/v2/hook/example", "secret"
        )

        with self.assertRaises(DeliveryError) as raised:
            FeishuClient(credentials).send_text("hello", timestamp=123)

        self.assertTrue(raised.exception.retryable)

    @patch(
        "codex_notify.feishu.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "https://open.feishu.cn/open-apis/bot/v2/hook/example",
            408,
            "Request Timeout",
            {},
            None,
        ),
    )
    def test_http_request_timeout_is_retryable(self, _urlopen):
        credentials = FeishuCredentials(
            "https://open.feishu.cn/open-apis/bot/v2/hook/example", "secret"
        )

        with self.assertRaises(DeliveryError) as raised:
            FeishuClient(credentials).send_text("hello", timestamp=123)

        self.assertTrue(raised.exception.retryable)

    @patch(
        "codex_notify.feishu.urllib.request.urlopen",
        return_value=_NonObjectResponse(),
    )
    def test_non_object_json_response_uses_retryable_delivery_error(self, _urlopen):
        credentials = FeishuCredentials(
            "https://open.feishu.cn/open-apis/bot/v2/hook/example", "secret"
        )

        with self.assertRaises(DeliveryError) as raised:
            FeishuClient(credentials).send_text("hello", timestamp=123)

        self.assertTrue(raised.exception.retryable)
        self.assertIn("JSON object", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
