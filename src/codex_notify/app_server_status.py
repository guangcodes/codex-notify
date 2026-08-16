"""Fail-silent, metadata-only reads of one exact Codex Turn status."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .app_server_metadata import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_STDOUT_BYTES,
    _LineReader,
    _terminate,
    find_bundled_codex,
)
from .paths import AppPaths


_TURN_LIST_LIMIT = 20
_TURN_STATUSES = {"completed", "failed", "interrupted", "inProgress"}
_TURN_FIELDS = {
    "id",
    "status",
    "startedAt",
    "completedAt",
    "durationMs",
    "error",
    "items",
    "itemsView",
}
_TURN_REQUIRED_FIELDS = {"id", "status", "items", "itemsView"}
_RESULT_FIELDS = {"data", "nextCursor", "backwardsCursor"}
_INVALID = object()
_SIMPLE_ERROR_CATEGORIES = {
    "contextWindowExceeded",
    "sessionBudgetExceeded",
    "usageLimitExceeded",
    "serverOverloaded",
    "cyberPolicy",
    "internalServerError",
    "unauthorized",
    "badRequest",
    "threadRollbackFailed",
    "sandboxError",
    "other",
}
_OBJECT_ERROR_CATEGORIES = {
    "httpConnectionFailed",
    "responseStreamConnectionFailed",
    "responseStreamDisconnected",
    "responseTooManyFailedAttempts",
    "activeTurnNotSteerable",
}
_HTTP_ERROR_CATEGORIES = _OBJECT_ERROR_CATEGORIES - {"activeTurnNotSteerable"}
_NON_STEERABLE_TURN_KINDS = {"review", "compact"}


@dataclass(frozen=True)
class TerminalStatus:
    turn_id: str
    status: str
    started_at: int | None
    completed_at: int | None
    duration_ms: int | None
    error_category: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "interrupted"}


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value and "\0" not in value and len(value) <= 4096 else None


def _optional_nonnegative_int(value: object) -> int | None | object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return _INVALID
    return value


def _error_category(value: object) -> str | None | object:
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value).issubset(
        {"message", "additionalDetails", "codexErrorInfo"}
    ):
        return _INVALID
    if not isinstance(value.get("message"), str):
        return _INVALID
    additional_details = value.get("additionalDetails")
    if additional_details is not None and not isinstance(additional_details, str):
        return _INVALID
    info = value.get("codexErrorInfo")
    if info is None:
        return None
    if isinstance(info, str):
        return info if info in _SIMPLE_ERROR_CATEGORIES else _INVALID
    if not isinstance(info, dict) or len(info) != 1:
        return _INVALID
    category = next(iter(info))
    if category not in _OBJECT_ERROR_CATEGORIES:
        return _INVALID
    details = info[category]
    if not isinstance(details, dict):
        return _INVALID
    if category in _HTTP_ERROR_CATEGORIES:
        if not set(details).issubset({"httpStatusCode"}):
            return _INVALID
        status_code = details.get("httpStatusCode")
        if status_code is not None and (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 0 <= status_code <= 65535
        ):
            return _INVALID
    elif set(details) != {"turnKind"} or details.get(
        "turnKind"
    ) not in _NON_STEERABLE_TURN_KINDS:
        return _INVALID
    return category


def _parse_turn(value: object) -> TerminalStatus | None:
    if (
        not isinstance(value, dict)
        or not _TURN_REQUIRED_FIELDS.issubset(value)
        or not set(value).issubset(_TURN_FIELDS)
    ):
        return None
    turn_id = _identifier(value.get("id"))
    status = value.get("status")
    if turn_id is None or status not in _TURN_STATUSES:
        return None
    if value.get("itemsView") != "notLoaded" or value.get("items") != []:
        return None
    started_at = _optional_nonnegative_int(value.get("startedAt"))
    completed_at = _optional_nonnegative_int(value.get("completedAt"))
    duration_ms = _optional_nonnegative_int(value.get("durationMs"))
    error_category = _error_category(value.get("error"))
    if any(
        item is _INVALID
        for item in (started_at, completed_at, duration_ms, error_category)
    ):
        return None
    return TerminalStatus(
        turn_id,
        status,
        started_at,
        completed_at,
        duration_ms,
        error_category,
    )


class AppServerStatusReader:
    def __init__(
        self,
        binary: Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._popen = popen

    def read(self, thread_id: str, turn_id: str) -> TerminalStatus | None:
        if _identifier(thread_id) is None or _identifier(turn_id) is None:
            return None
        process = None
        try:
            process = self._popen(
                [str(self.binary), "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            if process.stdin is None or process.stdout is None:
                return None
            deadline = time.monotonic() + self.timeout_seconds
            lines = _LineReader(process.stdout)
            total_bytes = 0
            request_id = 1

            def send(method: str, params: dict[str, object], *, notification: bool = False) -> None:
                payload: dict[str, object] = {"method": method, "params": params}
                if not notification:
                    payload["id"] = request_id
                process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                process.stdin.flush()

            def response(expected_id: int) -> dict[str, object] | None:
                nonlocal total_bytes
                while time.monotonic() < deadline:
                    line = lines.readline(deadline)
                    if line is None:
                        return None
                    total_bytes += len(line.encode("utf-8", errors="replace"))
                    if total_bytes > MAX_STDOUT_BYTES:
                        return None
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        return None
                    if not isinstance(message, dict) or message.get("id") != expected_id:
                        continue
                    result = message.get("result")
                    if "error" in message or not isinstance(result, dict):
                        return None
                    return result
                return None

            send(
                "initialize",
                {
                    "clientInfo": {"name": "codex-notify-status", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            if response(request_id) is None:
                return None
            send("initialized", {}, notification=True)

            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                request_id += 1
                params: dict[str, object] = {
                    "threadId": thread_id,
                    "limit": _TURN_LIST_LIMIT,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                }
                if cursor is not None:
                    params["cursor"] = cursor
                send("thread/turns/list", params)
                result = response(request_id)
                if result is None or not set(result).issubset(_RESULT_FIELDS):
                    return None
                data = result.get("data")
                if not isinstance(data, list):
                    return None
                found = None
                for raw_turn in data:
                    parsed = _parse_turn(raw_turn)
                    if parsed is None:
                        return None
                    if parsed.turn_id == turn_id:
                        found = parsed
                if found is not None:
                    return found
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    return None
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor in seen_cursors
                ):
                    return None
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except Exception:
            return None
        finally:
            if process is not None:
                _terminate(process)


def read_terminal_status(
    paths: AppPaths,
    thread_id: str,
    turn_id: str,
    *,
    binary: Path | None = None,
) -> TerminalStatus | None:
    binary = binary or find_bundled_codex()
    if binary is None:
        return None
    paths.ensure_runtime_dirs()
    lock_path = paths.data_dir / "app-server-probe.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return AppServerStatusReader(binary).read(thread_id, turn_id)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
