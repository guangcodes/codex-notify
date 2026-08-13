"""Feishu custom-bot signing and delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .keychain import FeishuCredentials


class DeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _retryable_http_status(status: int) -> bool:
    return status in {408, 425, 429} or status >= 500


def validate_webhook_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "open.feishu.cn"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
        or parsed.path == "/open-apis/bot/v2/hook/"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Webhook 必须是飞书 open.feishu.cn 自定义机器人 HTTPS 地址")
    return value


def make_signature(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@dataclass
class FeishuClient:
    credentials: FeishuCredentials
    timeout_seconds: float = 8.0

    def send_text(self, text: str, *, timestamp: int | None = None) -> None:
        webhook = validate_webhook_url(self.credentials.webhook_url)
        timestamp = timestamp if timestamp is not None else int(time.time())
        body = {
            "timestamp": str(timestamp),
            "sign": make_signature(timestamp, self.credentials.signing_secret),
            "msg_type": "text",
            "content": {"text": text},
        }
        request = urllib.request.Request(
            webhook,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(64 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            retryable = _retryable_http_status(exc.code)
            raise DeliveryError(f"飞书 HTTP {exc.code}", retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DeliveryError(f"飞书网络错误：{exc}", retryable=True) from exc

        if status != 200:
            raise DeliveryError(
                f"飞书 HTTP {status}",
                retryable=_retryable_http_status(status),
            )
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeliveryError("飞书返回了无效 JSON", retryable=True) from exc
        if not isinstance(result, dict):
            raise DeliveryError("飞书返回的 JSON object 无效", retryable=True)
        code = result.get("code", result.get("StatusCode"))
        if code not in (0, "0"):
            message = str(result.get("msg", result.get("StatusMessage", "未知错误")))
            # Custom-bot throttling can be reported as HTTP 200 with a nonzero
            # body code. Without a stable exhaustive code taxonomy, retain the
            # durable event and retry body-level rejections until retention
            # expires instead of permanently discarding a transient failure.
            raise DeliveryError(
                f"飞书拒绝请求：code={code}, msg={message}",
                retryable=True,
            )
