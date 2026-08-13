"""Handle Codex's definitive agent-turn-complete notification event."""

from __future__ import annotations

import json
from typing import Any

from .db import HookEvent, NotificationStore
from .logging_utils import append_error


def process_notification(
    payload: dict[str, Any],
    store: NotificationStore | None = None,
) -> bool:
    if payload.get("type") != "agent-turn-complete":
        return False
    event = HookEvent(
        session_id=str(payload.get("thread-id") or ""),
        turn_id=str(payload.get("turn-id") or ""),
        cwd=str(payload.get("cwd") or ""),
        last_assistant_message=str(payload.get("last-assistant-message") or ""),
    )
    return (store or NotificationStore()).record_completion(event)


def notification_main(raw_payload: str) -> int:
    try:
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            raise ValueError("notify payload 必须是 JSON object")
        process_notification(payload)
    except Exception as exc:
        # Codex notifications are advisory. Queue failures must not affect Codex.
        try:
            append_error(f"notify: {exc}")
        except Exception:
            pass
        return 0
    return 0
