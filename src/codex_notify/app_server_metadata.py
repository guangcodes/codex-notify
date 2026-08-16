"""Fail-silent, metadata-only reads from a one-shot Codex App Server."""

from __future__ import annotations

import fcntl
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from .paths import AppPaths


MAX_STDOUT_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
_INTERACTIVE_SOURCES = {"vscode", "appServer", "cli"}
_INTERACTIVE_SOURCE_ORDER = ["vscode", "appServer", "cli"]
_THREAD_LIST_LIMIT = 20


@dataclass(frozen=True)
class ThreadMetadata:
    thread_id: str
    parent_thread_id: str | None
    source_kind: str
    created_at: int | None

    @property
    def is_confirmed_root(self) -> bool:
        return self.parent_thread_id is None and self.source_kind in _INTERACTIVE_SOURCES


def find_bundled_codex(
    *, application_roots: tuple[Path, ...] | None = None
) -> Path | None:
    roots = (
        (Path("/Applications"), Path.home() / "Applications")
        if application_roots is None
        else application_roots
    )
    for root in roots:
        candidate = root / "ChatGPT.app" / "Contents" / "Resources" / "codex"
        try:
            if candidate.is_file() and not candidate.is_symlink() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _source_kind(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "subAgent" in value:
        return "subAgent"
    return ""


class AppServerMetadataReader:
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

    def read(
        self,
        thread_ids: list[str],
        *,
        expected_turn_ids: dict[str, str],
    ) -> dict[str, ThreadMetadata]:
        unique_ids = list(dict.fromkeys(value for value in thread_ids if value))
        if not unique_ids:
            return {}
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
                return {}
            deadline = time.monotonic() + self.timeout_seconds
            total_bytes = 0
            request_id = 1
            lines = _LineReader(process.stdout)

            def send(payload: dict[str, object]) -> None:
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
                    if "error" in message or not isinstance(message.get("result"), dict):
                        return None
                    return message["result"]
                return None

            send(
                {
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "codex-notify-metadata",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            if response(request_id) is None:
                return {}
            send({"method": "initialized", "params": {}})

            def request(method: str, params: dict[str, object]) -> dict[str, object] | None:
                nonlocal request_id
                request_id += 1
                send(
                    {
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                return response(request_id)

            def read_thread(
                thread_id: str, *, expected_session_id: str | None = None
            ) -> ThreadMetadata | None:
                envelope = request(
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": False},
                )
                if envelope is None:
                    return None
                thread = envelope.get("thread")
                if not isinstance(thread, dict) or thread.get("id") != thread_id:
                    return None
                if (
                    expected_session_id is not None
                    and thread.get("sessionId") != expected_session_id
                ):
                    return None
                if "parentThreadId" not in thread or "source" not in thread:
                    return None
                parent = thread["parentThreadId"]
                if parent is not None and not (isinstance(parent, str) and parent):
                    return None
                created = thread.get("createdAt")
                if not isinstance(created, int):
                    return None
                return ThreadMetadata(
                    thread_id=thread_id,
                    parent_thread_id=parent,
                    source_kind=_source_kind(thread.get("source")),
                    created_at=created,
                )

            def thread_contains_turn(thread_id: str, expected_turn_id: str) -> bool:
                cursor: str | None = None
                seen_cursors: set[str] = set()
                while True:
                    params: dict[str, object] = {
                        "threadId": thread_id,
                        "limit": _THREAD_LIST_LIMIT,
                        "sortDirection": "desc",
                        "itemsView": "notLoaded",
                    }
                    if cursor is not None:
                        params["cursor"] = cursor
                    listing = request("thread/turns/list", params)
                    data = listing.get("data") if listing is not None else None
                    if not isinstance(data, list):
                        return False
                    found = False
                    for turn in data:
                        if (
                            not isinstance(turn, dict)
                            or turn.get("itemsView") != "notLoaded"
                            or turn.get("items") != []
                        ):
                            return False
                        if turn.get("id") == expected_turn_id:
                            found = True
                    if found:
                        return True
                    next_cursor = listing.get("nextCursor")
                    if next_cursor is None:
                        return False
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor in seen_cursors
                    ):
                        return False
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor

            result: dict[str, ThreadMetadata] = {}
            unresolved: list[str] = []
            for hook_session_id in unique_ids:
                metadata = read_thread(hook_session_id)
                if metadata is None:
                    unresolved.append(hook_session_id)
                    continue
                expected_turn_id = expected_turn_ids.get(hook_session_id)
                if (
                    isinstance(expected_turn_id, str)
                    and expected_turn_id
                    and thread_contains_turn(hook_session_id, expected_turn_id)
                ):
                    result[hook_session_id] = metadata

            if unresolved:
                candidates: dict[str, set[str]] = {
                    session_id: set() for session_id in unresolved
                }
                cursor: str | None = None
                seen_cursors: set[str] = set()
                listing_complete = False
                while True:
                    params: dict[str, object] = {
                        "limit": _THREAD_LIST_LIMIT,
                        "sortKey": "updated_at",
                        "sortDirection": "desc",
                        "sourceKinds": _INTERACTIVE_SOURCE_ORDER,
                        "useStateDbOnly": True,
                    }
                    if cursor is not None:
                        params["cursor"] = cursor
                    listing = request("thread/list", params)
                    data = listing.get("data") if listing is not None else None
                    if not isinstance(data, list):
                        break
                    for thread in data:
                        if not isinstance(thread, dict):
                            continue
                        session_id = thread.get("sessionId")
                        thread_id = thread.get("id")
                        source_kind = _source_kind(thread.get("source"))
                        if (
                            session_id in candidates
                            and isinstance(thread_id, str)
                            and thread_id
                            and source_kind in _INTERACTIVE_SOURCES
                        ):
                            candidates[session_id].add(thread_id)
                    next_cursor = listing.get("nextCursor")
                    if next_cursor is None:
                        listing_complete = True
                        break
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor in seen_cursors
                    ):
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                if listing_complete:
                    for hook_session_id, thread_ids in candidates.items():
                        if len(thread_ids) != 1:
                            continue
                        expected_turn_id = expected_turn_ids.get(hook_session_id)
                        if not isinstance(expected_turn_id, str) or not expected_turn_id:
                            continue
                        thread_id = next(iter(thread_ids))
                        metadata = read_thread(
                            thread_id,
                            expected_session_id=hook_session_id,
                        )
                        if metadata is not None and thread_contains_turn(
                            thread_id, expected_turn_id
                        ):
                            result[hook_session_id] = metadata
            return result
        except Exception:
            return {}
        finally:
            if process is not None:
                _terminate(process)


class _LineReader:
    def __init__(self, stream: TextIO) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()

        def read() -> None:
            total_bytes = 0
            try:
                while True:
                    line = stream.readline(MAX_STDOUT_BYTES + 2)
                    if not line:
                        break
                    total_bytes += len(line.encode("utf-8"))
                    if total_bytes > MAX_STDOUT_BYTES:
                        break
                    self._lines.put(line)
            except Exception:
                pass
            finally:
                self._lines.put(None)

        threading.Thread(
            target=read,
            name="codex-notify-metadata-reader",
            daemon=True,
        ).start()

    def readline(self, deadline: float) -> str | None:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return self._lines.get(timeout=remaining)
        except queue.Empty:
            return None


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def read_pending_metadata(
    paths: AppPaths,
    thread_ids: list[str],
    expected_turn_ids: dict[str, str],
) -> dict[str, ThreadMetadata] | None:
    binary = find_bundled_codex()
    if binary is None or not thread_ids:
        return {}
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
        return AppServerMetadataReader(binary).read(
            thread_ids,
            expected_turn_ids=expected_turn_ids,
        )
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
