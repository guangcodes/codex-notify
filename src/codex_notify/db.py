"""SQLite-backed turn lifecycle state and reliable notification outbox."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import CLAIM_LEASE_SECONDS, OUTBOX_RETENTION_SECONDS, PENDING_CONFIRMATION_SECONDS
from .paths import AppPaths
from .redact import safe_summary


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    project TEXT NOT NULL,
    prompt_summary TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL,
    notify_pair INTEGER NOT NULL CHECK (notify_pair IN (0, 1)),
    suppressed INTEGER NOT NULL DEFAULT 0 CHECK (suppressed IN (0, 1)),
    state TEXT NOT NULL,
    classification TEXT NOT NULL DEFAULT 'UNVERIFIED',
    lifecycle TEXT NOT NULL DEFAULT 'RUNNING',
    suppression_reason TEXT,
    decision_reason TEXT NOT NULL DEFAULT '',
    decision_due_at REAL,
    pending_completed_at REAL,
    pending_completion_summary TEXT NOT NULL DEFAULT '',
    pending_completion_enabled INTEGER,
    classification_source TEXT NOT NULL DEFAULT 'public_contract',
    PRIMARY KEY (session_id, turn_id)
);

CREATE TRIGGER IF NOT EXISTS codex_notify_scrub_turn_cwd_after_insert
AFTER INSERT ON turns
WHEN NEW.cwd <> ''
BEGIN
    UPDATE turns SET cwd=''
    WHERE session_id=NEW.session_id AND turn_id=NEW.turn_id;
END;

CREATE TRIGGER IF NOT EXISTS codex_notify_scrub_turn_cwd_after_update
AFTER UPDATE OF cwd ON turns
WHEN NEW.cwd <> ''
BEGIN
    UPDATE turns SET cwd=''
    WHERE session_id=NEW.session_id AND turn_id=NEW.turn_id;
END;

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    session_id TEXT,
    turn_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    sent_at REAL,
    last_error TEXT,
    depends_on_event_key TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_due
ON outbox(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subagents (
    agent_id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    parent_turn_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    stopped_at REAL,
    state TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subagents_parent
ON subagents(parent_session_id, parent_turn_id);
"""

_CWD_SCRUB_SETTING = "raw_cwd_scrubbed_v1"
_SCHEMA_VERSION = 5


@dataclass(frozen=True)
class HookEvent:
    session_id: str
    turn_id: str
    cwd: str
    prompt: str = ""
    last_assistant_message: str = ""
    source: str = ""

    @property
    def project(self) -> str:
        path = Path(self.cwd)
        return safe_summary(path.name or str(path) or "未知项目", 120)


@dataclass(frozen=True)
class SubagentEvent:
    agent_id: str
    agent_type: str
    parent_session_id: str
    parent_turn_id: str


class NotificationStore:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.default()

    def connect(self) -> sqlite3.Connection:
        self.paths.ensure_runtime_dirs()
        with self._database_initialization_lock():
            connection = sqlite3.connect(self.paths.database, timeout=5.0)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                self._migrate(connection)
                now = time.time()
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value, updated_at) "
                    "VALUES('enabled', '0', ?)",
                    (now,),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value, updated_at) "
                    "VALUES('delivery_paused', '0', ?)",
                    (now,),
                )
                scrubbed = connection.execute(
                    "SELECT 1 FROM settings WHERE key=?", (_CWD_SCRUB_SETTING,)
                ).fetchone()
                if scrubbed is None:
                    connection.execute("UPDATE turns SET cwd='' WHERE cwd<>''")
                    connection.execute(
                        "INSERT INTO settings(key, value, updated_at) VALUES(?, '1', ?)",
                        (_CWD_SCRUB_SETTING, now),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                connection.close()
                raise
        try:
            self.paths.database.chmod(0o600)
        except FileNotFoundError:
            pass
        return connection

    @contextmanager
    def _database_initialization_lock(self) -> Iterator[None]:
        lock_path = self.paths.data_dir / "database-init.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        turn_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(turns)")
        }
        additions = {
            "classification": "TEXT NOT NULL DEFAULT 'UNVERIFIED'",
            "lifecycle": "TEXT NOT NULL DEFAULT 'RUNNING'",
            "suppression_reason": "TEXT",
            "decision_reason": "TEXT NOT NULL DEFAULT ''",
            "decision_due_at": "REAL",
            "pending_completed_at": "REAL",
            "pending_completion_summary": "TEXT NOT NULL DEFAULT ''",
            "pending_completion_enabled": "INTEGER",
            "classification_source": "TEXT NOT NULL DEFAULT 'public_contract'",
        }
        for name, declaration in additions.items():
            if name not in turn_columns:
                connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {declaration}")
        outbox_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(outbox)")
        }
        if "depends_on_event_key" not in outbox_columns:
            connection.execute("ALTER TABLE outbox ADD COLUMN depends_on_event_key TEXT")

        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < _SCHEMA_VERSION:
            connection.execute(
                """UPDATE turns
                   SET classification=CASE classification
                       WHEN 'USER_ROOT' THEN 'NOTIFIABLE_ROOT'
                       WHEN 'UNKNOWN' THEN 'NOTIFIABLE_ROOT'
                       WHEN 'PENDING' THEN CASE
                           WHEN state='completed' THEN 'NOTIFIABLE_ROOT'
                           ELSE 'PENDING_ROOT_CANDIDATE' END
                       WHEN 'RELATED_CHILD' THEN 'CONFIRMED_CHILD'
                       WHEN 'OTHER_INTERNAL' THEN 'CONFIRMED_CHILD'
                       WHEN 'UNVERIFIED' THEN 'NOTIFIABLE_ROOT'
                       ELSE classification END,
                       completed_at=COALESCE(completed_at, pending_completed_at),
                       lifecycle=CASE
                           WHEN state='completed' OR pending_completed_at IS NOT NULL
                           THEN 'COMPLETED' ELSE 'RUNNING' END,
                       state=CASE
                           WHEN pending_completed_at IS NOT NULL THEN 'completed'
                           ELSE state END,
                       classification_source=CASE
                           WHEN classification IN ('RELATED_CHILD', 'OTHER_INTERNAL')
                           THEN 'legacy_inert_compatibility'
                           ELSE 'public_contract' END"""
            )
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @contextmanager
    def managed_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def is_enabled(self, connection: sqlite3.Connection | None = None) -> bool:
        owned = connection is None
        connection = connection or self.connect()
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='enabled'"
            ).fetchone()
            return bool(row and row["value"] == "1")
        finally:
            if owned:
                connection.close()

    def set_enabled(
        self, enabled: bool, *, immediate: bool = False, now: float | None = None
    ) -> None:
        now = now if now is not None else time.time()
        with self.delivery_lock():
            if not enabled and immediate:
                self._begin_immediate_off(now)
                self._finish_immediate_off()
            else:
                self._set_enabled(enabled, now=now)

    def _begin_immediate_off(self, now: float) -> None:
        with self.managed_connection() as connection:
            self._write_setting(connection, "enabled", "0", now)
            self._write_setting(connection, "delivery_paused", "1", now)
            connection.execute(
                """UPDATE turns
                   SET notify_pair=0, suppressed=1,
                       suppression_reason='suppressed by off --now'
                   WHERE lifecycle='RUNNING'
                      OR classification='PENDING_ROOT_CANDIDATE'"""
            )
            connection.execute(
                """UPDATE outbox
                   SET status='suppressed', last_error='suppressed by off --now'
                   WHERE status IN ('pending', 'retry')"""
            )

    def _finish_immediate_off(self) -> None:
        with self.managed_connection() as connection:
            connection.execute(
                """UPDATE outbox
                   SET status='suppressed', last_error='suppressed by off --now'
                   WHERE status IN ('pending', 'retry', 'sending')"""
            )

    def _set_enabled(self, enabled: bool, *, now: float) -> None:
        with self.managed_connection() as connection:
            self._write_setting(connection, "enabled", "1" if enabled else "0", now)
            if enabled:
                self._write_setting(connection, "delivery_paused", "0", now)
            else:
                connection.execute(
                    """UPDATE turns
                       SET notify_pair=0, pending_completion_enabled=0
                       WHERE classification='PENDING_ROOT_CANDIDATE'
                         AND suppressed=0"""
                )

    @staticmethod
    def _write_setting(
        connection: sqlite3.Connection, key: str, value: str, now: float
    ) -> None:
        connection.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE
               SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, now),
        )

    def record_session_start(
        self, session_id: str, source: str, *, now: float | None = None
    ) -> bool:
        if not _valid_identifier(session_id):
            return False
        now = now if now is not None else time.time()
        normalized_source = source if source in {"startup", "resume", "clear", "compact"} else ""
        with self.managed_connection() as connection:
            cursor = connection.execute(
                """INSERT INTO sessions(session_id, source, first_seen_at, last_seen_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE
                   SET source=excluded.source, last_seen_at=excluded.last_seen_at""",
                (session_id, normalized_source, now, now),
            )
            return cursor.rowcount == 1

    def record_start(self, event: HookEvent, *, now: float | None = None) -> bool:
        if not _valid_event_identity(event):
            return False
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO turns(
                       session_id, turn_id, cwd, project, prompt_summary, started_at,
                       notify_pair, suppressed, state, classification, lifecycle,
                       suppression_reason, decision_reason, decision_due_at,
                       classification_source
                   ) VALUES(?, ?, '', ?, ?, ?, ?, 0, 'running',
                            'PENDING_ROOT_CANDIDATE', 'RUNNING', NULL,
                            'awaiting_public_relation_window', ?, 'public_contract')""",
                (
                    event.session_id,
                    event.turn_id,
                    event.project,
                    safe_summary(event.prompt, 120),
                    now,
                    1 if self.is_enabled(connection) else 0,
                    now + PENDING_CONFIRMATION_SECONDS,
                ),
            )
        return False

    def record_subagent_start(
        self, event: SubagentEvent, *, now: float | None = None
    ) -> bool:
        if not _valid_subagent_event(event):
            return False
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM subagents WHERE agent_id=?", (event.agent_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO subagents(
                           agent_id, agent_type, parent_session_id, parent_turn_id,
                           started_at, state
                       ) VALUES(?, ?, ?, ?, ?, 'active')""",
                    (
                        event.agent_id,
                        safe_summary(event.agent_type, 80),
                        event.parent_session_id,
                        event.parent_turn_id,
                        now,
                    ),
                )
                return True
            exact = (
                existing["agent_type"] == safe_summary(event.agent_type, 80)
                and existing["parent_session_id"] == event.parent_session_id
                and existing["parent_turn_id"] == event.parent_turn_id
            )
            if not exact and existing["state"] != "conflict":
                connection.execute(
                    "UPDATE subagents SET state='conflict' WHERE agent_id=?",
                    (event.agent_id,),
                )
            return False

    def record_subagent_stop(
        self, event: SubagentEvent, *, now: float | None = None
    ) -> bool:
        if not _valid_subagent_event(event):
            return False
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM subagents WHERE agent_id=?", (event.agent_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO subagents(
                           agent_id, agent_type, parent_session_id, parent_turn_id,
                           started_at, stopped_at, state
                       ) VALUES(?, ?, ?, ?, ?, ?, 'stopped')""",
                    (
                        event.agent_id,
                        safe_summary(event.agent_type, 80),
                        event.parent_session_id,
                        event.parent_turn_id,
                        now,
                        now,
                    ),
                )
                return True
            exact = (
                existing["agent_type"] == safe_summary(event.agent_type, 80)
                and existing["parent_session_id"] == event.parent_session_id
                and existing["parent_turn_id"] == event.parent_turn_id
            )
            if not exact:
                connection.execute(
                    "UPDATE subagents SET state='conflict' WHERE agent_id=?",
                    (event.agent_id,),
                )
                return False
            cursor = connection.execute(
                """UPDATE subagents
                   SET stopped_at=COALESCE(stopped_at, ?), state='stopped'
                   WHERE agent_id=? AND state='active'""",
                (now, event.agent_id),
            )
            return cursor.rowcount == 1

    def finalize_pending(self, *, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        finalized = 0
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM turns
                   WHERE classification='PENDING_ROOT_CANDIDATE'
                     AND decision_due_at<=?
                   ORDER BY started_at""",
                (now,),
            ).fetchall()
            for turn in rows:
                connection.execute(
                    """UPDATE turns
                       SET classification='NOTIFIABLE_ROOT',
                           decision_reason='public_contract_fail_open',
                           decision_due_at=NULL
                       WHERE session_id=? AND turn_id=?
                         AND classification='PENDING_ROOT_CANDIDATE'""",
                    (turn["session_id"], turn["turn_id"]),
                )
                start_created = False
                if turn["notify_pair"] and not turn["suppressed"]:
                    start_created = self._enqueue_start(connection, turn)
                if turn["pending_completed_at"] is not None and not turn["suppressed"]:
                    if start_created or self._start_outbox_row(connection, turn) is not None:
                        self._enqueue_completion_for_turn(
                            connection,
                            turn,
                            turn["pending_completion_summary"],
                            turn["pending_completed_at"],
                            incomplete_lifecycle=False,
                            depends_on_start=True,
                        )
                    elif turn["pending_completion_enabled"]:
                        self._enqueue_completion_for_turn(
                            connection,
                            turn,
                            turn["pending_completion_summary"],
                            turn["pending_completed_at"],
                            incomplete_lifecycle=True,
                            depends_on_start=False,
                        )
                finalized += 1
        return finalized

    def _enqueue_start(self, connection: sqlite3.Connection, turn: sqlite3.Row) -> bool:
        event_key = _event_key(turn["session_id"], turn["turn_id"], "started")
        cursor = self._insert_outbox(
            connection,
            event_key=event_key,
            event_type="started",
            session_id=turn["session_id"],
            turn_id=turn["turn_id"],
            payload={
                "project": turn["project"],
                "turn_id": turn["turn_id"],
                "event_id": _delivery_id(event_key),
                "occurred_at": turn["started_at"],
                "summary": turn["prompt_summary"],
            },
            due_at=turn["started_at"],
            now=turn["started_at"],
        )
        return cursor.rowcount == 1

    @staticmethod
    def _start_outbox_row(
        connection: sqlite3.Connection, turn: sqlite3.Row
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM outbox
               WHERE session_id=? AND turn_id=? AND event_type='started'
               ORDER BY id LIMIT 1""",
            (turn["session_id"], turn["turn_id"]),
        ).fetchone()

    def record_completion(self, event: HookEvent, *, now: float | None = None) -> bool:
        if not _valid_event_identity(event):
            return False
        now = now if now is not None else time.time()
        result_summary = safe_summary(event.last_assistant_message, 300)
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            completion_enabled = self.is_enabled(connection)
            turn = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                (event.session_id, event.turn_id),
            ).fetchone()
            if turn is None:
                connection.execute(
                    """INSERT INTO turns(
                           session_id, turn_id, cwd, project, prompt_summary,
                           started_at, completed_at, notify_pair, suppressed,
                           state, classification, lifecycle, decision_reason,
                           classification_source
                       ) VALUES(?, ?, '', ?, '', ?, ?, 0, 0, 'completed',
                                'NOTIFIABLE_ROOT', 'COMPLETED',
                                'completion_without_start_fail_open',
                                'public_contract')""",
                    (event.session_id, event.turn_id, event.project, now, now),
                )
                if not completion_enabled:
                    return False
                self._enqueue_standalone_completion(
                    connection,
                    session_id=event.session_id,
                    turn_id=event.turn_id,
                    project=event.project,
                    summary=result_summary,
                    now=now,
                )
                return True

            existing_completion = connection.execute(
                """SELECT * FROM outbox
                   WHERE session_id=? AND turn_id=? AND event_type='completed'
                   ORDER BY id LIMIT 1""",
                (turn["session_id"], turn["turn_id"]),
            ).fetchone()
            if turn["lifecycle"] == "COMPLETED":
                return existing_completion is not None

            connection.execute(
                """UPDATE turns
                   SET completed_at=?, pending_completed_at=?,
                       pending_completion_summary=?, pending_completion_enabled=?,
                       lifecycle='COMPLETED', state='completed'
                   WHERE session_id=? AND turn_id=?""",
                (
                    now,
                    now,
                    result_summary,
                    1 if completion_enabled else 0,
                    turn["session_id"],
                    turn["turn_id"],
                ),
            )
            if turn["suppressed"]:
                return False
            if turn["classification"] == "PENDING_ROOT_CANDIDATE":
                return False
            if turn["classification"] in {"CONFIRMED_CHILD", "UNVERIFIED"}:
                return False

            start = self._start_outbox_row(connection, turn)
            if start is not None:
                if start["status"] == "suppressed":
                    return False
                self._enqueue_completion_for_turn(
                    connection,
                    turn,
                    result_summary,
                    now,
                    incomplete_lifecycle=False,
                    depends_on_start=True,
                )
                return True
            if not completion_enabled:
                return False
            self._enqueue_completion_for_turn(
                connection,
                turn,
                result_summary,
                now,
                incomplete_lifecycle=True,
                depends_on_start=False,
            )
            return True

    def _enqueue_standalone_completion(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str,
        project: str,
        summary: str,
        now: float,
    ) -> None:
        event_key = _event_key(session_id, turn_id, "completed")
        self._insert_outbox(
            connection,
            event_key=event_key,
            event_type="completed",
            session_id=session_id,
            turn_id=turn_id,
            payload={
                "project": project,
                "turn_id": turn_id,
                "event_id": _delivery_id(event_key),
                "occurred_at": now,
                "started_at": now,
                "duration_seconds": 0,
                "summary": summary,
                "incomplete_lifecycle": True,
            },
            due_at=now,
            now=now,
        )

    def _enqueue_completion_for_turn(
        self,
        connection: sqlite3.Connection,
        turn: sqlite3.Row,
        result_summary: str,
        completed_at: float,
        *,
        incomplete_lifecycle: bool,
        depends_on_start: bool,
    ) -> None:
        event_key = _event_key(turn["session_id"], turn["turn_id"], "completed")
        self._insert_outbox(
            connection,
            event_key=event_key,
            event_type="completed",
            session_id=turn["session_id"],
            turn_id=turn["turn_id"],
            payload={
                "project": turn["project"],
                "turn_id": turn["turn_id"],
                "event_id": _delivery_id(event_key),
                "occurred_at": completed_at,
                "started_at": turn["started_at"],
                "duration_seconds": max(0, int(completed_at - turn["started_at"])),
                "summary": result_summary,
                "incomplete_lifecycle": incomplete_lifecycle,
            },
            due_at=completed_at,
            now=completed_at,
            depends_on_event_key=(
                _event_key(turn["session_id"], turn["turn_id"], "started")
                if depends_on_start
                else None
            ),
        )

    def enqueue_test(self, *, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        event_key = f"test:{uuid.uuid4()}"
        with self.managed_connection() as connection:
            self._insert_outbox(
                connection,
                event_key=event_key,
                event_type="test",
                session_id=None,
                turn_id=None,
                payload={
                    "project": "codex-notify",
                    "occurred_at": now,
                    "summary": "测试通知",
                    "event_id": _delivery_id(event_key),
                },
                due_at=now,
                now=now,
            )
        return event_key

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        *,
        event_key: str,
        event_type: str,
        session_id: str | None,
        turn_id: str | None,
        payload: dict[str, Any],
        due_at: float,
        now: float,
        depends_on_event_key: str | None = None,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """INSERT OR IGNORE INTO outbox(
                   event_key, session_id, turn_id, event_type, payload_json,
                   status, next_attempt_at, created_at, depends_on_event_key
               ) VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (
                event_key,
                session_id,
                turn_id,
                event_type,
                json.dumps(payload, ensure_ascii=False),
                due_at,
                now,
                depends_on_event_key,
            ),
        )

    def claim_due(
        self, *, limit: int, now: float | None = None
    ) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE outbox
                   SET status='retry', last_error='recovered expired worker lease'
                   WHERE status='sending' AND next_attempt_at<=?""",
                (now,),
            )
            connection.execute(
                """UPDATE outbox AS dependent
                   SET status='suppressed', last_error='dependency was not delivered'
                   WHERE dependent.depends_on_event_key IS NOT NULL
                     AND dependent.status IN ('pending', 'retry')
                     AND EXISTS(
                         SELECT 1 FROM outbox AS dependency
                         WHERE dependency.event_key=dependent.depends_on_event_key
                           AND dependency.status IN ('dead', 'suppressed'))"""
            )
            rows = connection.execute(
                """SELECT * FROM outbox
                   WHERE status IN ('pending', 'retry') AND next_attempt_at<=?
                     AND (
                         depends_on_event_key IS NULL
                         OR NOT EXISTS(
                             SELECT 1 FROM outbox AS dependency
                             WHERE dependency.event_key=outbox.depends_on_event_key)
                         OR EXISTS(
                             SELECT 1 FROM outbox AS dependency
                             WHERE dependency.event_key=outbox.depends_on_event_key
                               AND dependency.status='sent'))
                   ORDER BY id LIMIT ?""",
                (now, limit),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"""UPDATE outbox
                        SET status='sending', attempts=attempts+1, next_attempt_at=?
                        WHERE id IN ({placeholders})""",
                    (now + CLAIM_LEASE_SECONDS, *ids),
                )
            connection.commit()
            return [
                {
                    **dict(row),
                    "attempts": row["attempts"] + 1,
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_test(
        self, event_key: str, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE outbox
                   SET status='retry', last_error='recovered expired worker lease'
                   WHERE event_key=? AND event_type='test'
                     AND status='sending' AND next_attempt_at<=?""",
                (event_key, now),
            )
            row = connection.execute(
                """SELECT * FROM outbox
                   WHERE event_key=? AND event_type='test'
                     AND status IN ('pending', 'retry') AND next_attempt_at<=?""",
                (event_key, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return []
            connection.execute(
                """UPDATE outbox
                   SET status='sending', attempts=attempts+1, next_attempt_at=?
                   WHERE id=?""",
                (now + CLAIM_LEASE_SECONDS, row["id"]),
            )
            connection.commit()
            return [
                {
                    **dict(row),
                    "attempts": row["attempts"] + 1,
                    "payload": json.loads(row["payload_json"]),
                }
            ]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_sent(self, item_id: int, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status='sent', sent_at=?, last_error=NULL
                   WHERE id=? AND status='sending'""",
                (now, item_id),
            )
            if cursor.rowcount != 1:
                return False
            self._write_setting(connection, "last_delivery_at", str(now), now)
            return True

    def mark_retry(self, item_id: int, error: str, next_attempt_at: float) -> None:
        with self.managed_connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status='retry', next_attempt_at=?, last_error=?
                   WHERE id=? AND status='sending'""",
                (next_attempt_at, safe_summary(error, 500), item_id),
            )
            if cursor.rowcount == 1:
                self._record_last_error(connection, error)

    def mark_dead(self, item_id: int, error: str) -> None:
        with self.managed_connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status='dead', last_error=?
                   WHERE id=? AND status='sending'""",
                (safe_summary(error, 500), item_id),
            )
            if cursor.rowcount == 1:
                self._record_last_error(connection, error)

    @staticmethod
    def _record_last_error(connection: sqlite3.Connection, error: str) -> None:
        now = time.time()
        NotificationStore._write_setting(
            connection, "last_error", safe_summary(error, 500), now
        )

    def event_status(self, event_key: str) -> str | None:
        with self.managed_connection() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE event_key=?", (event_key,)
            ).fetchone()
            return row["status"] if row else None

    def is_sendable(self, item_id: int) -> bool:
        with self.managed_connection() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE id=?", (item_id,)
            ).fetchone()
            return bool(row and row["status"] == "sending")

    def is_delivery_paused(self) -> bool:
        with self.managed_connection() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='delivery_paused'"
            ).fetchone()
            return bool(row and row["value"] == "1")

    def mark_suppressed(self, item_id: int, reason: str) -> bool:
        with self.managed_connection() as connection:
            cursor = connection.execute(
                """UPDATE outbox SET status='suppressed', last_error=?
                   WHERE id=? AND status='sending'""",
                (safe_summary(reason, 500), item_id),
            )
            return cursor.rowcount == 1

    @contextmanager
    def delivery_lock(self) -> Iterator[None]:
        self.paths.ensure_runtime_dirs()
        with self.paths.send_lock.open("a+", encoding="utf-8") as handle:
            self.paths.send_lock.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def status_snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM outbox GROUP BY status"
                )
            }
            settings = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM settings")
            }
            enabled = settings.get("enabled") == "1"
            active = connection.execute(
                """SELECT COUNT(*) AS count FROM turns
                   WHERE lifecycle='RUNNING' AND suppressed=0
                     AND classification IN (
                         'PENDING_ROOT_CANDIDATE', 'NOTIFIABLE_ROOT', 'CONFLICT')
                     AND started_at>?
                     AND (notify_pair=1 OR ?)""",
                (now - OUTBOX_RETENTION_SECONDS, 1 if enabled else 0),
            ).fetchone()["count"]
            pending_decisions = connection.execute(
                """SELECT COUNT(*) AS count FROM turns
                   WHERE classification='PENDING_ROOT_CANDIDATE' AND suppressed=0"""
            ).fetchone()["count"]
            return {
                "enabled": enabled,
                "active_turns": active,
                "pending": counts.get("pending", 0)
                + counts.get("retry", 0)
                + counts.get("sending", 0),
                "pending_decisions": pending_decisions,
                "dead": counts.get("dead", 0),
                "last_delivery_at": settings.get("last_delivery_at"),
                "last_error": settings.get("last_error"),
            }


def _event_key(session_id: str, turn_id: str, event_type: str) -> str:
    return json.dumps(
        [session_id, turn_id, event_type], ensure_ascii=False, separators=(",", ":")
    )


def _delivery_id(event_key: str) -> str:
    return hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:12]


def _valid_identifier(value: str) -> bool:
    return bool(value) and "\0" not in value and len(value) <= 4096


def _valid_event_identity(event: HookEvent) -> bool:
    return _valid_identifier(event.session_id) and _valid_identifier(event.turn_id)


def _valid_subagent_event(event: SubagentEvent) -> bool:
    return all(
        _valid_identifier(value)
        for value in (event.agent_id, event.parent_session_id, event.parent_turn_id)
    )
