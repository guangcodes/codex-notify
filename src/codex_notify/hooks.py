"""Codex hook entry points. Hook failures never block a turn."""

from __future__ import annotations

import json
import hashlib
import sys
from typing import Any, TextIO

from .db import (
    HookEvent,
    NotificationStore,
    PermissionEvent,
    RequestUserInputEvent,
    SubagentEvent,
)
from .logging_utils import append_error


def _event(payload: dict[str, Any]) -> HookEvent:
    return HookEvent(
        session_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        cwd=str(payload.get("cwd") or ""),
        prompt=str(payload.get("prompt") or ""),
        last_assistant_message=str(payload.get("last_assistant_message") or ""),
    )


def process_hook(
    event_name: str,
    payload: dict[str, Any],
    store: NotificationStore | None = None,
) -> None:
    store = store or NotificationStore()
    if event_name == "SessionStart":
        store.record_session_start(
            str(payload.get("session_id") or ""),
            str(payload.get("source") or ""),
        )
    elif event_name == "UserPromptSubmit":
        store.record_start(_event(payload))
    elif event_name in {"SubagentStart", "SubagentStop"}:
        relation = SubagentEvent(
            agent_id=str(payload.get("agent_id") or ""),
            agent_type=str(payload.get("agent_type") or ""),
            parent_session_id=str(payload.get("session_id") or ""),
            parent_turn_id=str(payload.get("turn_id") or ""),
        )
        if event_name == "SubagentStart":
            store.record_subagent_start(relation)
        else:
            store.record_subagent_stop(relation)
    elif event_name == "PermissionRequest":
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(canonical).hexdigest()
        store.record_permission_request(
            PermissionEvent(
                session_id=str(payload.get("session_id") or ""),
                turn_id=str(payload.get("turn_id") or ""),
                tool_name=str(payload.get("tool_name") or ""),
                event_fingerprint=fingerprint,
            )
        )
    elif event_name == "PreToolUse":
        tool_name = payload.get("tool_name")
        if tool_name != "request_user_input":
            return
        tool_use_id = payload.get("tool_use_id")
        if isinstance(tool_use_id, str) and tool_use_id and "\0" not in tool_use_id:
            fingerprint_source: object = [
                payload.get("session_id"),
                payload.get("turn_id"),
                tool_name,
                tool_use_id,
            ]
        else:
            fingerprint_source = payload
        canonical = json.dumps(
            fingerprint_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        store.record_request_user_input(
            RequestUserInputEvent(
                session_id=str(payload.get("session_id") or ""),
                turn_id=str(payload.get("turn_id") or ""),
                tool_name=tool_name,
                signal_fingerprint=hashlib.sha256(canonical).hexdigest(),
            )
        )
    else:
        raise ValueError(f"不支持的 Hook：{event_name}")


def hook_main(
    event_name: str,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.load(stdin)
        if not isinstance(payload, dict):
            raise ValueError("Hook payload 必须是 JSON object")
        process_hook(event_name, payload)
    except Exception as exc:
        try:
            append_error(f"{event_name}: {exc}")
        except Exception:
            pass
    if event_name == "SubagentStop":
        stdout.write("{}\n")
        stdout.flush()
    return 0
