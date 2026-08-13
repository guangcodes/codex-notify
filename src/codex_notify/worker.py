"""Reliable outbox worker."""

from __future__ import annotations

import time
from collections.abc import Callable

from .constants import DEFAULT_BATCH_SIZE, OUTBOX_RETENTION_SECONDS, RETRY_DELAYS_SECONDS
from .db import NotificationStore
from .feishu import DeliveryError, FeishuClient
from .keychain import load_credentials
from .messages import render_message


def _retry_delay(attempts: int) -> int:
    index = min(max(1, attempts) - 1, len(RETRY_DELAYS_SECONDS) - 1)
    return RETRY_DELAYS_SECONDS[index]


def run_once(
    store: NotificationStore | None = None,
    *,
    client_factory: Callable[[], FeishuClient] | None = None,
    now: float | None = None,
    event_key: str | None = None,
) -> int:
    store = store or NotificationStore()
    store.finalize_pending(now=now)
    now = now if now is not None else time.time()
    items = (
        store.claim_test(event_key, now=now)
        if event_key is not None
        else store.claim_due(limit=DEFAULT_BATCH_SIZE, now=now)
    )
    if not items:
        return 0
    client_factory = client_factory or (lambda: FeishuClient(load_credentials()))
    client: FeishuClient | None = None
    delivered = 0
    for item in items:
        try:
            with store.delivery_lock():
                if item["event_type"] != "test" and store.is_delivery_paused():
                    store.mark_suppressed(item["id"], "suppressed by off --now")
                    continue
                if not store.is_sendable(item["id"]):
                    continue
                if now - float(item["created_at"]) >= OUTBOX_RETENTION_SECONDS:
                    store.mark_dead(item["id"], "通知已超过 24 小时重试期限")
                    continue
                client = client or client_factory()
                client.send_text(render_message(item["event_type"], item["payload"]))
                if store.mark_sent(item["id"], now=now):
                    delivered += 1
        except DeliveryError as exc:
            if not exc.retryable:
                store.mark_dead(item["id"], str(exc))
            else:
                store.mark_retry(item["id"], str(exc), now + _retry_delay(item["attempts"]))
        except Exception as exc:
            store.mark_retry(item["id"], str(exc), now + _retry_delay(item["attempts"]))
    return delivered
