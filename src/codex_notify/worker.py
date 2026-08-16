"""Reliable outbox worker."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .app_server_metadata import ThreadMetadata, read_pending_metadata
from .app_server_status import TerminalStatus, read_terminal_status
from .experimental_status import ExperimentalSnapshot, read_experimental_status
from .constants import (
    DEFAULT_BATCH_SIZE,
    OUTBOX_RETENTION_SECONDS,
    RETRY_DELAYS_SECONDS,
    TERMINAL_SCAN_BATCH_SIZE,
)
from .db import NotificationStore
from .feishu import DeliveryError, FeishuClient
from .keychain import load_credentials
from .messages import render_message
from .paths import AppPaths


def _retry_delay(attempts: int) -> int:
    index = min(max(1, attempts) - 1, len(RETRY_DELAYS_SECONDS) - 1)
    return RETRY_DELAYS_SECONDS[index]


def run_once(
    store: NotificationStore | None = None,
    *,
    client_factory: Callable[[], FeishuClient] | None = None,
    now: float | None = None,
    event_key: str | None = None,
    metadata_reader: Callable[
        [AppPaths, list[str], dict[str, str]], dict[str, ThreadMetadata] | None
    ]
    | None = None,
    status_reader: Callable[[AppPaths, str, str], TerminalStatus | None] | None = None,
    experimental_reader: Callable[
        [AppPaths, set[str]], ExperimentalSnapshot
    ]
    | None = None,
) -> int:
    store = store or NotificationStore()
    metadata_turns: list[tuple[str, str]] | None = None
    if event_key is None and store.is_enabled():
        metadata_turns = store.pending_metadata_turns(now=now, limit=1)
        metadata_session_ids = [session_id for session_id, _turn_id in metadata_turns]
        metadata_turn_ids = dict(metadata_turns)
        if metadata_turns:
            try:
                reader = metadata_reader or read_pending_metadata
                metadata = reader(store.paths, metadata_session_ids, metadata_turn_ids)
            except Exception:
                metadata = {}
            if metadata is None:
                metadata_turns = []
            else:
                for session_id, item in metadata.items():
                    store.record_thread_metadata(
                        session_id,
                        turn_id=metadata_turn_ids.get(session_id),
                        app_thread_id=item.thread_id,
                        parent_thread_id=item.parent_thread_id,
                        source_kind=item.source_kind,
                        now=now,
                    )
    if event_key is None:
        store.finalize_pending(now=now, turn_keys=metadata_turns)
    now = now if now is not None else time.time()
    items = (
        store.claim_test(event_key, now=now)
        if event_key is not None
        else store.claim_due(limit=DEFAULT_BATCH_SIZE, now=now)
    )
    delivered = 0
    claimed = len(items)
    client: FeishuClient | None = None

    def deliver(batch: list[dict[str, Any]]) -> None:
        nonlocal client, delivered, client_factory
        if batch:
            client_factory = client_factory or (lambda: FeishuClient(load_credentials()))
        for item in batch:
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

    deliver(items)
    if event_key is None:
        reader = status_reader or read_terminal_status
        for session_id, turn_id, app_thread_id in store.pending_terminal_turns(
            now=now, limit=TERMINAL_SCAN_BATCH_SIZE
        ):
            try:
                status = reader(store.paths, app_thread_id, turn_id)
            except Exception:
                status = None
            store.record_terminal_probe(session_id, turn_id, status, now=now)
        store.finalize_aggregations(now=now, require_due_probe=True)
        # Serialize the entire read-and-record window with ``off --now``.  If the
        # read wins, immediate shutdown suppresses the resulting current state;
        # if shutdown wins, the feature re-check below prevents a stale read.
        with store.delivery_lock():
            experimental_features = store.experimental_query_features(now=now)
            if experimental_features:
                store.mark_experimental_query_attempt(now=now)
                try:
                    reader = experimental_reader or read_experimental_status
                    snapshot = reader(store.paths, experimental_features)
                except Exception:
                    snapshot = ExperimentalSnapshot()
                store.record_experimental_snapshot(snapshot, now=now)
        if claimed == 0:
            deliver(
                store.claim_due(
                    limit=DEFAULT_BATCH_SIZE,
                    now=now,
                    dependent_only=True,
                )
            )
    return delivered
