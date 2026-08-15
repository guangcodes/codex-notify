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
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import (
    CLAIM_LEASE_SECONDS,
    FINAL_AGGREGATION_SECONDS,
    MAX_CHILD_RESULT_LENGTH,
    MAX_CHILD_RESULTS,
    OUTBOX_RETENTION_SECONDS,
    PENDING_CONFIRMATION_SECONDS,
)
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
    parent_session_id TEXT,
    parent_turn_id TEXT,
    relation_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    relation_source TEXT NOT NULL DEFAULT '',
    aggregation_due_at REAL,
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
    state TEXT NOT NULL,
    resolved_parent_session_id TEXT,
    resolved_parent_turn_id TEXT,
    relation_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    relation_source TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_subagents_parent
ON subagents(parent_session_id, parent_turn_id);
"""

_CWD_SCRUB_SETTING = "raw_cwd_scrubbed_v1"
_SCHEMA_VERSION = 6


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
        probe = sqlite3.connect(self.paths.database, timeout=5.0)
        try:
            current_version = probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
        migration_lock = (
            self._delivery_file_lock()
            if current_version < _SCHEMA_VERSION
            else nullcontext()
        )
        with migration_lock:
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
                            "INSERT INTO settings(key, value, updated_at) "
                            "VALUES(?, '1', ?)",
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
            "parent_session_id": "TEXT",
            "parent_turn_id": "TEXT",
            "relation_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "relation_source": "TEXT NOT NULL DEFAULT ''",
            "aggregation_due_at": "REAL",
        }
        for name, declaration in additions.items():
            if name not in turn_columns:
                connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {declaration}")
        outbox_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(outbox)")
        }
        if "depends_on_event_key" not in outbox_columns:
            connection.execute("ALTER TABLE outbox ADD COLUMN depends_on_event_key TEXT")

        subagent_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(subagents)")
        }
        subagent_additions = {
            "resolved_parent_session_id": "TEXT",
            "resolved_parent_turn_id": "TEXT",
            "relation_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "relation_source": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in subagent_additions.items():
            if name not in subagent_columns:
                connection.execute(f"ALTER TABLE subagents ADD COLUMN {name} {declaration}")

        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 5:
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
                           ELSE 'public_contract' END,
                       decision_reason=CASE
                           WHEN classification IN (
                               'USER_ROOT', 'UNKNOWN', 'PENDING', 'UNVERIFIED'
                           ) THEN 'public_contract_fail_open'
                           WHEN decision_reason<>'' THEN decision_reason
                           ELSE decision_reason END"""
            )
        if current_version < 6:
            connection.execute(
                """UPDATE turns
                   SET decision_reason='public_contract_fail_open'
                   WHERE classification='NOTIFIABLE_ROOT'
                     AND decision_reason=''
                     AND classification_source='public_contract'"""
            )
            connection.execute(
                """UPDATE outbox
                   SET status='suppressed',
                       last_error='suppressed by v6 unknown-origin policy'
                   WHERE status IN ('pending', 'retry', 'sending')
                     AND EXISTS(
                         SELECT 1 FROM turns
                         WHERE turns.session_id=outbox.session_id
                           AND turns.turn_id=outbox.turn_id
                           AND turns.decision_reason IN (
                               'public_contract_fail_open',
                               'completion_without_start_fail_open'
                           )
                           AND NOT EXISTS(
                               SELECT 1 FROM outbox AS sent_start
                               WHERE sent_start.session_id=turns.session_id
                                 AND sent_start.turn_id=turns.turn_id
                                 AND sent_start.event_type='started'
                                 AND sent_start.status='sent'
                           )
                     )"""
            )
            connection.execute(
                """UPDATE turns
                   SET classification='UNVERIFIED',
                       decision_reason='legacy_fail_open_suppressed',
                       classification_source='v6_policy_migration'
                   WHERE decision_reason IN (
                       'public_contract_fail_open',
                       'completion_without_start_fail_open'
                   )
                     AND NOT EXISTS(
                         SELECT 1 FROM outbox
                         WHERE outbox.session_id=turns.session_id
                           AND outbox.turn_id=turns.turn_id
                           AND outbox.event_type='started'
                           AND outbox.status='sent'
                     )"""
            )
            connection.execute(
                """UPDATE turns
                   SET decision_reason='legacy_sent_start_pair',
                       classification_source='v6_policy_migration',
                       aggregation_due_at=CASE
                           WHEN lifecycle='COMPLETED'
                            AND NOT EXISTS(
                                SELECT 1 FROM outbox AS completion
                                WHERE completion.session_id=turns.session_id
                                  AND completion.turn_id=turns.turn_id
                                  AND completion.event_type='completed'
                            )
                           THEN COALESCE(
                               completed_at,
                               pending_completed_at,
                               started_at
                           )
                           ELSE aggregation_due_at
                       END
                   WHERE decision_reason='public_contract_fail_open'
                     AND EXISTS(
                         SELECT 1 FROM outbox
                         WHERE outbox.session_id=turns.session_id
                           AND outbox.turn_id=turns.turn_id
                           AND outbox.event_type='started'
                           AND outbox.status='sent'
                     )"""
            )
        if current_version < _SCHEMA_VERSION:
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
                       suppression_reason='suppressed by off --now',
                       aggregation_due_at=NULL
                   WHERE lifecycle='RUNNING'
                      OR classification='PENDING_ROOT_CANDIDATE'
                      OR aggregation_due_at IS NOT NULL"""
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
            relation = self._exact_child_relation(connection, event, now)
            classification = "CONFIRMED_CHILD" if relation is not None else "PENDING_ROOT_CANDIDATE"
            parent_session_id = (
                relation["resolved_parent_session_id"] if relation is not None else None
            )
            parent_turn_id = (
                relation["resolved_parent_turn_id"] if relation is not None else None
            )
            connection.execute(
                """INSERT OR IGNORE INTO turns(
                       session_id, turn_id, cwd, project, prompt_summary, started_at,
                       notify_pair, suppressed, state, classification, lifecycle,
                       suppression_reason, decision_reason, decision_due_at,
                       classification_source, parent_session_id, parent_turn_id,
                       relation_state, relation_source
                   ) VALUES(?, ?, '', ?, ?, ?, ?, 0, 'running',
                            ?, 'RUNNING', NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.session_id,
                    event.turn_id,
                    event.project,
                    safe_summary(event.prompt, 120),
                    now,
                    1 if self.is_enabled(connection) else 0,
                    classification,
                    (
                        "hook_exact_child_identity"
                        if relation is not None
                        else "awaiting_metadata_or_relation"
                    ),
                    None if relation is not None else now + PENDING_CONFIRMATION_SECONDS,
                    "hook_exact_child_identity" if relation is not None else "public_contract",
                    parent_session_id,
                    parent_turn_id,
                    "CONFIRMED" if relation is not None else "UNKNOWN",
                    "hook_exact_child_identity" if relation is not None else "",
                ),
            )
        return False

    @staticmethod
    def _exact_child_relation(
        connection: sqlite3.Connection, event: HookEvent, now: float
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM subagents
               WHERE agent_id=? AND parent_turn_id=?
                 AND relation_state='INFERRED_HIGH'
                 AND state<>'conflict'
                 AND started_at<=?
                 AND resolved_parent_session_id IS NOT NULL
                 AND resolved_parent_turn_id IS NOT NULL""",
            (event.session_id, event.turn_id, now),
        ).fetchone()

    def record_subagent_start(
        self, event: SubagentEvent, *, now: float | None = None
    ) -> bool:
        if not _valid_subagent_event(event):
            return False
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent_candidates = connection.execute(
                """SELECT session_id, turn_id FROM turns
                   WHERE session_id=? AND lifecycle='RUNNING' AND started_at<=?
                   ORDER BY started_at""",
                (event.parent_session_id, now),
            ).fetchall()
            if len(parent_candidates) == 1:
                resolved_parent_session_id = parent_candidates[0]["session_id"]
                resolved_parent_turn_id = parent_candidates[0]["turn_id"]
                relation_state = "INFERRED_HIGH"
                relation_source = "hook_unique_active_turn"
            elif len(parent_candidates) > 1:
                resolved_parent_session_id = None
                resolved_parent_turn_id = None
                relation_state = "CONFLICT"
                relation_source = ""
            else:
                resolved_parent_session_id = None
                resolved_parent_turn_id = None
                relation_state = "UNKNOWN"
                relation_source = ""
            existing = connection.execute(
                "SELECT * FROM subagents WHERE agent_id=?", (event.agent_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO subagents(
                           agent_id, agent_type, parent_session_id, parent_turn_id,
                           started_at, state, resolved_parent_session_id,
                           resolved_parent_turn_id, relation_state, relation_source
                       ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                    (
                        event.agent_id,
                        safe_summary(event.agent_type, 80),
                        event.parent_session_id,
                        event.parent_turn_id,
                        now,
                        resolved_parent_session_id,
                        resolved_parent_turn_id,
                        relation_state,
                        relation_source,
                    ),
                )
                self._classify_existing_child_turn(connection, event, now)
                return True
            exact = (
                existing["agent_type"] == safe_summary(event.agent_type, 80)
                and existing["parent_session_id"] == event.parent_session_id
                and existing["parent_turn_id"] == event.parent_turn_id
            )
            if not exact and existing["state"] != "conflict":
                connection.execute(
                    """UPDATE subagents
                       SET state='conflict', relation_state='CONFLICT',
                           relation_source=''
                       WHERE agent_id=?""",
                    (event.agent_id,),
                )
                connection.execute(
                    """UPDATE turns
                       SET classification='CONFLICT', relation_state='CONFLICT',
                           relation_source=''
                       WHERE session_id=?
                         AND turn_id IN (?, ?)
                         AND classification IN (
                             'PENDING_ROOT_CANDIDATE', 'CONFIRMED_CHILD'
                         )""",
                    (
                        event.agent_id,
                        existing["parent_turn_id"],
                        event.parent_turn_id,
                    ),
                )
            return False

    @staticmethod
    def _classify_existing_child_turn(
        connection: sqlite3.Connection, event: SubagentEvent, now: float
    ) -> None:
        relation = connection.execute(
            "SELECT * FROM subagents WHERE agent_id=?", (event.agent_id,)
        ).fetchone()
        if relation is None or relation["relation_state"] != "INFERRED_HIGH":
            return
        connection.execute(
            """UPDATE turns
               SET classification='CONFIRMED_CHILD', decision_due_at=NULL,
                   decision_reason='hook_exact_child_identity',
                   classification_source='hook_exact_child_identity',
                   parent_session_id=?, parent_turn_id=?,
                   relation_state='CONFIRMED',
                   relation_source='hook_exact_child_identity'
               WHERE session_id=? AND turn_id=? AND started_at>=?
                 AND classification='PENDING_ROOT_CANDIDATE'""",
            (
                relation["resolved_parent_session_id"],
                relation["resolved_parent_turn_id"],
                event.agent_id,
                event.parent_turn_id,
                now,
            ),
        )

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
                    """UPDATE subagents
                       SET state='conflict', relation_state='CONFLICT',
                           relation_source=''
                       WHERE agent_id=?""",
                    (event.agent_id,),
                )
                connection.execute(
                    """UPDATE turns
                       SET classification='CONFLICT', relation_state='CONFLICT',
                           relation_source=''
                       WHERE session_id=?
                         AND turn_id IN (?, ?)
                         AND classification IN (
                             'PENDING_ROOT_CANDIDATE', 'CONFIRMED_CHILD'
                         )""",
                    (
                        event.agent_id,
                        existing["parent_turn_id"],
                        event.parent_turn_id,
                    ),
                )
                return False
            cursor = connection.execute(
                """UPDATE subagents
                   SET stopped_at=COALESCE(stopped_at, ?), state='stopped'
                   WHERE agent_id=? AND state='active'""",
                (now, event.agent_id),
            )
            return cursor.rowcount == 1

    def finalize_pending(
        self,
        *,
        now: float | None = None,
        turn_keys: list[tuple[str, str]] | None = None,
    ) -> int:
        now = now if now is not None else time.time()
        if turn_keys == []:
            return 0
        finalized = 0
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn_filter = ""
            parameters: list[Any] = [now]
            if turn_keys is not None:
                predicates = " OR ".join(
                    "(session_id=? AND turn_id=?)" for _key in turn_keys
                )
                turn_filter = f" AND ({predicates})"
                for session_id, turn_id in turn_keys:
                    parameters.extend((session_id, turn_id))
            rows = connection.execute(
                f"""SELECT * FROM turns
                   WHERE classification='PENDING_ROOT_CANDIDATE'
                     AND decision_due_at<=?
                     {turn_filter}
                   ORDER BY started_at""",
                parameters,
            ).fetchall()
            for turn in rows:
                confirmed_root = turn["decision_reason"] == "metadata_confirmed_root"
                classification = "NOTIFIABLE_ROOT" if confirmed_root else "UNVERIFIED"
                decision_reason = (
                    "metadata_confirmed_root"
                    if confirmed_root
                    else "metadata_or_relation_unavailable"
                )
                connection.execute(
                    """UPDATE turns
                       SET classification=?, decision_reason=?, decision_due_at=NULL
                       WHERE session_id=? AND turn_id=?
                         AND classification='PENDING_ROOT_CANDIDATE'""",
                    (
                        classification,
                        decision_reason,
                        turn["session_id"],
                        turn["turn_id"],
                    ),
                )
                start_created = False
                if confirmed_root and turn["notify_pair"] and not turn["suppressed"]:
                    start_created = self._enqueue_start(connection, turn)
                if (
                    confirmed_root
                    and turn["pending_completed_at"] is not None
                    and not turn["suppressed"]
                    and (
                        start_created
                        or self._start_outbox_row(connection, turn) is not None
                    )
                ):
                    connection.execute(
                        """UPDATE turns SET aggregation_due_at=?
                           WHERE session_id=? AND turn_id=?""",
                        (
                            turn["pending_completed_at"] + FINAL_AGGREGATION_SECONDS,
                            turn["session_id"],
                            turn["turn_id"],
                        ),
                    )
                finalized += 1
        return finalized

    def pending_metadata_turns(
        self, *, now: float | None = None, limit: int | None = None
    ) -> list[tuple[str, str]]:
        now = now if now is not None else time.time()
        limit_clause = "" if limit is None else " LIMIT ?"
        parameters: list[Any] = [now]
        if limit is not None:
            parameters.append(max(0, limit))
        with self.managed_connection() as connection:
            return [
                (row["session_id"], row["turn_id"])
                for row in connection.execute(
                    f"""SELECT session_id, turn_id FROM turns
                       WHERE classification='PENDING_ROOT_CANDIDATE'
                         AND decision_due_at<=? AND suppressed=0
                       ORDER BY started_at{limit_clause}""",
                    parameters,
                )
            ]

    def record_thread_metadata(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        parent_thread_id: str | None,
        source_kind: str,
        now: float | None = None,
    ) -> bool:
        if not _valid_identifier(session_id):
            return False
        if turn_id is not None and not _valid_identifier(turn_id):
            return False
        now = now if now is not None else time.time()
        confirmed_root = parent_thread_id is None and source_kind in {
            "vscode",
            "appServer",
            "cli",
        }
        with self.managed_connection() as connection:
            turn_filter = "" if turn_id is None else " AND turn_id=?"
            parameters: list[Any] = [
                "metadata_confirmed_root" if confirmed_root else "metadata_not_root",
                "CONFIRMED" if confirmed_root else "UNKNOWN",
                session_id,
            ]
            if turn_id is not None:
                parameters.append(turn_id)
            cursor = connection.execute(
                f"""UPDATE turns
                   SET decision_reason=?, classification_source='app_server_metadata',
                       relation_state=?, relation_source='app_server_metadata'
                   WHERE session_id=?
                     AND classification='PENDING_ROOT_CANDIDATE'{turn_filter}""",
                parameters,
            )
            return cursor.rowcount > 0

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
            relation = self._exact_child_relation(connection, event, now)
            turn = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                (event.session_id, event.turn_id),
            ).fetchone()
            if turn is None:
                is_child = relation is not None
                connection.execute(
                    """INSERT INTO turns(
                           session_id, turn_id, cwd, project, prompt_summary,
                           started_at, completed_at, notify_pair, suppressed,
                           state, classification, lifecycle, decision_reason,
                           classification_source, pending_completed_at,
                           pending_completion_summary, pending_completion_enabled,
                           parent_session_id, parent_turn_id, relation_state,
                           relation_source
                       ) VALUES(?, ?, '', ?, '', ?, ?, 0, 0, 'completed',
                                ?, 'COMPLETED', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.session_id,
                        event.turn_id,
                        event.project,
                        now,
                        now,
                        "CONFIRMED_CHILD" if is_child else "UNVERIFIED",
                        (
                            "hook_exact_child_identity"
                            if is_child
                            else "completion_without_confirmed_origin"
                        ),
                        "hook_exact_child_identity" if is_child else "public_contract",
                        now,
                        result_summary,
                        1 if completion_enabled else 0,
                        relation["resolved_parent_session_id"] if is_child else None,
                        relation["resolved_parent_turn_id"] if is_child else None,
                        "CONFIRMED" if is_child else "UNKNOWN",
                        "hook_exact_child_identity" if is_child else "",
                    ),
                )
                return False

            existing_completion = connection.execute(
                """SELECT * FROM outbox
                   WHERE session_id=? AND turn_id=? AND event_type='completed'
                   ORDER BY id LIMIT 1""",
                (turn["session_id"], turn["turn_id"]),
            ).fetchone()
            if turn["lifecycle"] == "COMPLETED":
                return existing_completion is not None

            start = self._start_outbox_row(connection, turn)
            preserve_root_pair = (
                relation is not None
                and turn["classification"] == "NOTIFIABLE_ROOT"
                and start is not None
            )
            relation_conflict = (
                relation is not None
                and turn["classification"] == "NOTIFIABLE_ROOT"
                and start is None
            )
            apply_child_relation = relation is not None and turn["classification"] in {
                "PENDING_ROOT_CANDIDATE",
                "UNVERIFIED",
                "CONFIRMED_CHILD",
            }
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
            if apply_child_relation:
                connection.execute(
                    """UPDATE turns
                       SET classification='CONFIRMED_CHILD', decision_due_at=NULL,
                           parent_session_id=?, parent_turn_id=?,
                           relation_state='CONFIRMED',
                           relation_source='hook_exact_child_identity',
                           classification_source='hook_exact_child_identity'
                       WHERE session_id=? AND turn_id=?""",
                    (
                        relation["resolved_parent_session_id"],
                        relation["resolved_parent_turn_id"],
                        turn["session_id"],
                        turn["turn_id"],
                    ),
                )
            elif relation_conflict:
                connection.execute(
                    """UPDATE turns
                       SET classification='CONFLICT', decision_due_at=NULL,
                           relation_state='CONFLICT', relation_source=''
                       WHERE session_id=? AND turn_id=?""",
                    (turn["session_id"], turn["turn_id"]),
                )
            if turn["suppressed"]:
                return False
            effective_classification = turn["classification"]
            if apply_child_relation:
                effective_classification = "CONFIRMED_CHILD"
            elif relation_conflict:
                effective_classification = "CONFLICT"
            if effective_classification == "PENDING_ROOT_CANDIDATE":
                return False
            if effective_classification in {"CONFIRMED_CHILD", "UNVERIFIED", "CONFLICT"}:
                return False

            if start is not None and start["status"] == "suppressed":
                return False
            if start is None:
                return False
            connection.execute(
                """UPDATE turns SET aggregation_due_at=?
                   WHERE session_id=? AND turn_id=?""",
                (
                    now + FINAL_AGGREGATION_SECONDS,
                    turn["session_id"],
                    turn["turn_id"],
                ),
            )
            return False

    def finalize_aggregations(self, *, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        finalized = 0
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            roots = connection.execute(
                """SELECT * FROM turns
                   WHERE classification='NOTIFIABLE_ROOT'
                     AND lifecycle='COMPLETED'
                     AND aggregation_due_at IS NOT NULL
                     AND aggregation_due_at<=?
                   ORDER BY completed_at""",
                (now,),
            ).fetchall()
            for root in roots:
                existing = connection.execute(
                    """SELECT 1 FROM outbox
                       WHERE session_id=? AND turn_id=? AND event_type='completed'""",
                    (root["session_id"], root["turn_id"]),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        """UPDATE turns SET aggregation_due_at=NULL
                           WHERE session_id=? AND turn_id=?""",
                        (root["session_id"], root["turn_id"]),
                    )
                    continue
                children = connection.execute(
                    """SELECT turns.pending_completion_summary, subagents.agent_type,
                              subagents.started_at
                       FROM turns
                       JOIN subagents ON subagents.agent_id=turns.session_id
                                    AND subagents.parent_turn_id=turns.turn_id
                       WHERE turns.parent_session_id=? AND turns.parent_turn_id=?
                         AND turns.classification='CONFIRMED_CHILD'
                         AND turns.lifecycle='COMPLETED'
                         AND turns.completed_at<=?
                         AND turns.relation_state='CONFIRMED'
                         AND subagents.relation_state='INFERRED_HIGH'
                         AND subagents.state<>'conflict'
                       ORDER BY subagents.started_at, turns.session_id, turns.turn_id""",
                    (
                        root["session_id"],
                        root["turn_id"],
                        root["aggregation_due_at"],
                    ),
                ).fetchall()
                child_results = [
                    {
                        "agent_type": safe_summary(child["agent_type"], 80) or "subagent",
                        "summary": safe_summary(
                            child["pending_completion_summary"], MAX_CHILD_RESULT_LENGTH
                        )
                        or "（无可用结果）",
                    }
                    for child in children[:MAX_CHILD_RESULTS]
                ]
                omitted = max(0, len(children) - len(child_results))
                start = self._start_outbox_row(connection, root)
                self._enqueue_completion_for_turn(
                    connection,
                    root,
                    root["pending_completion_summary"],
                    float(root["completed_at"] or now),
                    incomplete_lifecycle=start is None,
                    depends_on_start=start is not None,
                    child_results=child_results,
                    omitted_child_results=omitted,
                    due_at=now,
                )
                connection.execute(
                    """UPDATE turns SET aggregation_due_at=NULL
                       WHERE session_id=? AND turn_id=?""",
                    (root["session_id"], root["turn_id"]),
                )
                finalized += 1
        return finalized

    def _enqueue_completion_for_turn(
        self,
        connection: sqlite3.Connection,
        turn: sqlite3.Row,
        result_summary: str,
        completed_at: float,
        *,
        incomplete_lifecycle: bool,
        depends_on_start: bool,
        child_results: list[dict[str, str]] | None = None,
        omitted_child_results: int = 0,
        due_at: float | None = None,
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
                "child_results": child_results or [],
                "omitted_child_results": omitted_child_results,
            },
            due_at=completed_at if due_at is None else due_at,
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
        connection = self.connect()
        connection.close()
        with self._delivery_file_lock():
            yield

    @contextmanager
    def _delivery_file_lock(self) -> Iterator[None]:
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
            unverified_turns = connection.execute(
                "SELECT COUNT(*) AS count FROM turns WHERE classification='UNVERIFIED'"
            ).fetchone()["count"]
            conflict_relations = connection.execute(
                """SELECT COUNT(*) AS count FROM (
                       SELECT agent_id AS relation_id FROM subagents
                       WHERE relation_state='CONFLICT' OR state='conflict'
                       UNION
                       SELECT session_id AS relation_id FROM turns
                       WHERE classification='CONFLICT' OR relation_state='CONFLICT'
                   )"""
            ).fetchone()["count"]
            return {
                "enabled": enabled,
                "active_turns": active,
                "pending": counts.get("pending", 0)
                + counts.get("retry", 0)
                + counts.get("sending", 0),
                "pending_decisions": pending_decisions,
                "unverified_turns": unverified_turns,
                "conflict_relations": conflict_relations,
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
