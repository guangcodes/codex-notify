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
    TERMINAL_CALIBRATION_SECONDS,
    TERMINAL_SCAN_RETRY_SECONDS,
)
from .experimental_status import (
    EXPERIMENTAL_FEATURES,
    FEATURE_MCP_AUTH,
    FEATURE_RATE_LIMITS,
    ExperimentalSnapshot,
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
    app_thread_id TEXT,
    terminal_status TEXT,
    terminal_source TEXT NOT NULL DEFAULT '',
    terminal_error_category TEXT,
    terminal_started_at REAL,
    terminal_completed_at REAL,
    terminal_duration_ms INTEGER,
    terminal_check_attempts INTEGER NOT NULL DEFAULT 0,
    terminal_check_due_at REAL,
    terminal_calibration_deadline REAL,
    terminal_scan_stopped_at REAL,
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

CREATE TABLE IF NOT EXISTS experimental_signal_state (
    feature TEXT NOT NULL,
    signal_key TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'turn')),
    certainty TEXT NOT NULL DEFAULT 'best_effort',
    signal_source TEXT NOT NULL,
    signal_kind TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    last_state TEXT NOT NULL,
    observed_at REAL NOT NULL,
    last_success_at REAL NOT NULL,
    transition_count INTEGER NOT NULL DEFAULT 0,
    signal_id TEXT,
    cooldown_key TEXT,
    last_notified_cooldown_key TEXT,
    last_notified_at REAL,
    PRIMARY KEY (feature, signal_key)
);
"""

_CWD_SCRUB_SETTING = "raw_cwd_scrubbed_v1"
_SCHEMA_VERSION = 8

_EXPERIMENTAL_EVENT_TYPES = {
    "request-user-input": "experimental_request_user_input",
    FEATURE_MCP_AUTH: "experimental_mcp_auth",
    FEATURE_RATE_LIMITS: "experimental_rate_limit",
}


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


@dataclass(frozen=True)
class PermissionEvent:
    session_id: str
    turn_id: str
    tool_name: str
    event_fingerprint: str
    reason: str = ""


@dataclass(frozen=True)
class RequestUserInputEvent:
    session_id: str
    turn_id: str
    tool_name: str
    signal_fingerprint: str


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
                    for feature in EXPERIMENTAL_FEATURES:
                        connection.execute(
                            "INSERT OR IGNORE INTO settings(key, value, updated_at) "
                            "VALUES(?, '0', ?)",
                            (f"experimental.{feature}.enabled", now),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO settings(key, value, updated_at) "
                            "VALUES(?, 'unprobed', ?)",
                            (f"experimental.{feature}.capability", now),
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
            "app_thread_id": "TEXT",
            "terminal_status": "TEXT",
            "terminal_source": "TEXT NOT NULL DEFAULT ''",
            "terminal_error_category": "TEXT",
            "terminal_started_at": "REAL",
            "terminal_completed_at": "REAL",
            "terminal_duration_ms": "INTEGER",
            "terminal_check_attempts": "INTEGER NOT NULL DEFAULT 0",
            "terminal_check_due_at": "REAL",
            "terminal_calibration_deadline": "REAL",
            "terminal_scan_stopped_at": "REAL",
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

        experimental_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(experimental_signal_state)"
            )
        }
        experimental_additions = {
            "certainty": "TEXT NOT NULL DEFAULT 'best_effort'",
            "signal_source": "TEXT NOT NULL DEFAULT ''",
            "signal_kind": "TEXT NOT NULL DEFAULT ''",
            "display_name": "TEXT NOT NULL DEFAULT ''",
            "transition_count": "INTEGER NOT NULL DEFAULT 0",
            "signal_id": "TEXT",
            "cooldown_key": "TEXT",
            "last_notified_cooldown_key": "TEXT",
            "last_notified_at": "REAL",
        }
        for name, declaration in experimental_additions.items():
            if name not in experimental_columns:
                connection.execute(
                    f"ALTER TABLE experimental_signal_state ADD COLUMN {name} {declaration}"
                )

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
        if current_version < 7:
            connection.execute(
                """UPDATE turns
                   SET terminal_status='completed',
                       terminal_source='legacy_completed',
                       terminal_started_at=started_at,
                       terminal_completed_at=COALESCE(completed_at, pending_completed_at),
                       terminal_duration_ms=CASE
                           WHEN COALESCE(completed_at, pending_completed_at) IS NOT NULL
                           THEN MAX(0, CAST((COALESCE(completed_at, pending_completed_at)
                                            - started_at) * 1000 AS INTEGER))
                           ELSE NULL END
                   WHERE lifecycle='COMPLETED'
                     AND terminal_status IS NULL"""
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
                      OR aggregation_due_at IS NOT NULL
                      OR terminal_calibration_deadline IS NOT NULL
                      OR terminal_check_due_at IS NOT NULL"""
            )
            connection.execute(
                """UPDATE outbox
                   SET status='suppressed', last_error='suppressed by off --now'
                   WHERE status IN ('pending', 'retry')"""
            )
            connection.execute(
                """UPDATE experimental_signal_state
                   SET last_notified_cooldown_key=cooldown_key,
                       last_notified_at=?
                   WHERE cooldown_key IS NOT NULL""",
                (now,),
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
                    """UPDATE outbox
                       SET status='suppressed',
                           last_error='notifications disabled before root confirmation'
                       WHERE event_type='experimental_request_user_input'
                         AND status IN ('pending', 'retry', 'sending')
                         AND EXISTS(
                             SELECT 1 FROM turns
                             WHERE turns.session_id=outbox.session_id
                               AND turns.turn_id=outbox.turn_id
                               AND turns.classification='PENDING_ROOT_CANDIDATE')"""
                )
                connection.execute(
                    """UPDATE turns
                       SET notify_pair=0, pending_completion_enabled=0
                       WHERE classification='PENDING_ROOT_CANDIDATE'
                         AND suppressed=0"""
                )

    def set_experimental_capability(
        self, feature: str, available: bool, reason: str, *, now: float | None = None
    ) -> None:
        _require_experimental_feature(feature)
        now = now if now is not None else time.time()
        with self.delivery_lock():
            with self.managed_connection() as connection:
                self._write_setting(
                    connection,
                    f"experimental.{feature}.capability",
                    "available" if available else "unavailable",
                    now,
                )
                self._write_setting(
                    connection,
                    f"experimental.{feature}.reason",
                    safe_summary(reason, 200),
                    now,
                )
                if not available:
                    self._write_setting(
                        connection, f"experimental.{feature}.enabled", "0", now
                    )
                    self._suppress_experimental_outbox(
                        connection, feature, "experimental capability unavailable"
                    )

    def set_experimental_enabled(
        self, feature: str, enabled: bool, *, now: float | None = None
    ) -> None:
        _require_experimental_feature(feature)
        now = now if now is not None else time.time()
        with self.delivery_lock():
            with self.managed_connection() as connection:
                if enabled:
                    row = connection.execute(
                        "SELECT value FROM settings WHERE key=?",
                        (f"experimental.{feature}.capability",),
                    ).fetchone()
                    if row is None or row["value"] != "available":
                        raise ValueError(f"实验功能 {feature} 当前 unavailable，不能启用")
                self._write_setting(
                    connection,
                    f"experimental.{feature}.enabled",
                    "1" if enabled else "0",
                    now,
                )
                if not enabled:
                    self._suppress_experimental_outbox(
                        connection, feature, "experimental feature disabled"
                    )

    @staticmethod
    def _suppress_experimental_outbox(
        connection: sqlite3.Connection, feature: str, reason: str
    ) -> None:
        connection.execute(
            """UPDATE outbox
               SET status='suppressed', last_error=?
               WHERE event_type=? AND status IN ('pending', 'retry', 'sending')""",
            (reason, _EXPERIMENTAL_EVENT_TYPES[feature]),
        )

    def is_experimental_enabled(
        self, feature: str, connection: sqlite3.Connection | None = None
    ) -> bool:
        _require_experimental_feature(feature)
        owned = connection is None
        connection = connection or self.connect()
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key=?",
                (f"experimental.{feature}.enabled",),
            ).fetchone()
            return bool(row and row["value"] == "1")
        finally:
            if owned:
                connection.close()

    def experimental_feature_status(self) -> dict[str, dict[str, str | bool]]:
        with self.managed_connection() as connection:
            settings = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM settings WHERE key LIKE 'experimental.%'"
                )
            }
        return {
            feature: {
                "enabled": settings.get(f"experimental.{feature}.enabled") == "1",
                "capability": settings.get(
                    f"experimental.{feature}.capability", "unprobed"
                ),
                "reason": settings.get(f"experimental.{feature}.reason", "尚未探测"),
            }
            for feature in EXPERIMENTAL_FEATURES
        }

    def experimental_query_features(self, *, now: float | None = None) -> set[str]:
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            if not self.is_enabled(connection):
                return set()
            next_row = connection.execute(
                "SELECT value FROM settings WHERE key='experimental.query.next_at'"
            ).fetchone()
            try:
                next_at = float(next_row["value"]) if next_row else 0.0
            except (TypeError, ValueError):
                next_at = 0.0
            if next_at > now:
                return set()
            return {
                feature
                for feature in (FEATURE_MCP_AUTH, FEATURE_RATE_LIMITS)
                if self.is_experimental_enabled(feature, connection)
            }

    def mark_experimental_query_attempt(self, *, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            self._write_setting(connection, "experimental.query.last_attempt_at", str(now), now)
            self._write_setting(connection, "experimental.query.next_at", str(now + 60), now)

    def record_experimental_snapshot(
        self, snapshot: ExperimentalSnapshot, *, now: float | None = None
    ) -> int:
        now = now if now is not None else time.time()
        queued = 0
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if snapshot.mcp_auth is not None:
                self._write_setting(
                    connection, "experimental.mcp-auth.last_success_at", str(now), now
                )
                observed_mcp_keys = {
                    observation.signal_key for observation in snapshot.mcp_auth
                }
                for observation in snapshot.mcp_auth:
                    row = connection.execute(
                        "SELECT * FROM experimental_signal_state WHERE feature=? AND signal_key=?",
                        (FEATURE_MCP_AUTH, observation.signal_key),
                    ).fetchone()
                    previous_state = row["last_state"] if row is not None else None
                    transition_count = int(row["transition_count"]) if row is not None else 0
                    if previous_state != observation.auth_status:
                        transition_count += 1
                    last_notified = (
                        row["last_notified_cooldown_key"] if row is not None else None
                    )
                    indeterminate_states = {"notLoggedIn", "unknown", "unsupported"}
                    preserves_active_episode = (
                        observation.auth_status in indeterminate_states
                        and previous_state in indeterminate_states
                        and last_notified is not None
                    )
                    cooldown_key = (
                        last_notified
                        if preserves_active_episode
                        else f"notLoggedIn:{transition_count}"
                    )
                    if observation.auth_status in {"bearerToken", "oAuth"}:
                        last_notified = None
                    signal_id = hashlib.sha256(
                        f"{observation.signal_key}:{transition_count}".encode("utf-8")
                    ).hexdigest()[:16]
                    connection.execute(
                        """INSERT INTO experimental_signal_state(
                               feature, signal_key, scope, certainty, signal_source,
                               signal_kind, display_name, last_state,
                               observed_at, last_success_at, transition_count, signal_id,
                               cooldown_key, last_notified_cooldown_key, last_notified_at
                           ) VALUES(?, ?, 'global', 'best_effort', 'app_server_status',
                                    'mcp_auth_not_logged_in', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(feature, signal_key) DO UPDATE SET
                               certainty=excluded.certainty,
                               signal_source=excluded.signal_source,
                               signal_kind=excluded.signal_kind,
                               display_name=excluded.display_name,
                               last_state=excluded.last_state,
                               observed_at=excluded.observed_at,
                               last_success_at=excluded.last_success_at,
                               transition_count=excluded.transition_count,
                               signal_id=excluded.signal_id,
                               cooldown_key=excluded.cooldown_key,
                               last_notified_cooldown_key=excluded.last_notified_cooldown_key,
                               last_notified_at=excluded.last_notified_at""",
                        (
                            FEATURE_MCP_AUTH,
                            observation.signal_key,
                            observation.display_name,
                            observation.auth_status,
                            now,
                            now,
                            transition_count,
                            signal_id,
                            cooldown_key,
                            last_notified,
                            row["last_notified_at"] if row is not None else None,
                        ),
                    )
                    if (
                        observation.auth_status == "notLoggedIn"
                        and last_notified != cooldown_key
                        and self.is_enabled(connection)
                        and self.is_experimental_enabled(FEATURE_MCP_AUTH, connection)
                    ):
                        event_key = _event_key(
                            "global", observation.signal_key, f"mcp-auth:{cooldown_key}"
                        )
                        cursor = self._insert_outbox(
                            connection,
                            event_key=event_key,
                            event_type="experimental_mcp_auth",
                            session_id=None,
                            turn_id=None,
                            payload=_experimental_payload(
                                event_key,
                                now,
                                "app_server_status",
                                "mcp_auth_not_logged_in",
                                signal_id,
                                display_name=observation.display_name,
                            ),
                            due_at=now,
                            now=now,
                        )
                        if cursor.rowcount == 1:
                            queued += 1
                            connection.execute(
                                """UPDATE experimental_signal_state
                                   SET last_notified_cooldown_key=?, last_notified_at=?
                                   WHERE feature=? AND signal_key=?""",
                                (
                                    cooldown_key,
                                    now,
                                    FEATURE_MCP_AUTH,
                                    observation.signal_key,
                                ),
                            )
                missing_filter = ""
                missing_parameters: list[Any] = [now, now, FEATURE_MCP_AUTH]
                if observed_mcp_keys:
                    placeholders = ",".join("?" for _key in observed_mcp_keys)
                    missing_filter = f" AND signal_key NOT IN ({placeholders})"
                    missing_parameters.extend(sorted(observed_mcp_keys))
                connection.execute(
                    f"""UPDATE experimental_signal_state
                        SET last_state='absent',
                            observed_at=?,
                            last_success_at=?,
                            transition_count=transition_count+1,
                            signal_id=NULL,
                            cooldown_key=NULL,
                            last_notified_cooldown_key=NULL
                        WHERE feature=? AND last_state!='absent'
                        {missing_filter}""",
                    missing_parameters,
                )
            if snapshot.rate_limits is not None:
                self._write_setting(
                    connection, "experimental.rate-limits.last_success_at", str(now), now
                )
                for observation in snapshot.rate_limits:
                    row = connection.execute(
                        "SELECT * FROM experimental_signal_state WHERE feature=? AND signal_key=?",
                        (FEATURE_RATE_LIMITS, observation.signal_key),
                    ).fetchone()
                    state = observation.reached_type or "normal"
                    transition_count = int(row["transition_count"]) if row is not None else 0
                    if row is None or row["last_state"] != state:
                        transition_count += 1
                    last_notified = (
                        row["last_notified_cooldown_key"] if row is not None else None
                    )
                    signal_id = observation.cooldown_key[:16]
                    connection.execute(
                        """INSERT INTO experimental_signal_state(
                               feature, signal_key, scope, certainty, signal_source,
                               signal_kind, last_state, observed_at,
                               last_success_at, transition_count, signal_id, cooldown_key,
                               last_notified_cooldown_key, last_notified_at
                           ) VALUES(?, ?, 'global', 'best_effort', 'app_server_status',
                                    'account_rate_limit_reached', ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(feature, signal_key) DO UPDATE SET
                               certainty=excluded.certainty,
                               signal_source=excluded.signal_source,
                               signal_kind=excluded.signal_kind,
                               last_state=excluded.last_state,
                               observed_at=excluded.observed_at,
                               last_success_at=excluded.last_success_at,
                               transition_count=excluded.transition_count,
                               signal_id=excluded.signal_id,
                               cooldown_key=excluded.cooldown_key,
                               last_notified_cooldown_key=excluded.last_notified_cooldown_key,
                               last_notified_at=excluded.last_notified_at""",
                        (
                            FEATURE_RATE_LIMITS,
                            observation.signal_key,
                            state,
                            now,
                            now,
                            transition_count,
                            signal_id,
                            observation.cooldown_key,
                            last_notified,
                            row["last_notified_at"] if row is not None else None,
                        ),
                    )
                    if (
                        observation.reached_type is not None
                        and last_notified != observation.cooldown_key
                        and self.is_enabled(connection)
                        and self.is_experimental_enabled(FEATURE_RATE_LIMITS, connection)
                    ):
                        event_key = _event_key(
                            "global",
                            observation.signal_key,
                            f"rate-limit:{observation.cooldown_key}",
                        )
                        cursor = self._insert_outbox(
                            connection,
                            event_key=event_key,
                            event_type="experimental_rate_limit",
                            session_id=None,
                            turn_id=None,
                            payload=_experimental_payload(
                                event_key,
                                now,
                                "app_server_status",
                                "account_rate_limit_reached",
                                signal_id,
                            ),
                            due_at=now,
                            now=now,
                        )
                        if cursor.rowcount == 1:
                            queued += 1
                            connection.execute(
                                """UPDATE experimental_signal_state
                                   SET last_notified_cooldown_key=?, last_notified_at=?
                                   WHERE feature=? AND signal_key=?""",
                                (
                                    observation.cooldown_key,
                                    now,
                                    FEATURE_RATE_LIMITS,
                                    observation.signal_key,
                                ),
                            )
            if snapshot.mcp_auth is not None or snapshot.rate_limits is not None:
                self._write_setting(
                    connection, "experimental.query.last_success_at", str(now), now
                )
                self._write_setting(
                    connection, "experimental.query.next_at", str(now + 300), now
                )
        return queued

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

    def record_permission_request(
        self, event: PermissionEvent, *, now: float | None = None
    ) -> bool:
        if not _valid_permission_event(event):
            return False
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                (event.session_id, event.turn_id),
            ).fetchone()
            if source is None:
                return False
            if source["lifecycle"] != "RUNNING" or source["terminal_status"] is not None:
                return False
            is_child = source["classification"] == "CONFIRMED_CHILD"
            root = source
            if is_child:
                if (
                    source["relation_state"] != "CONFIRMED"
                    or not source["parent_session_id"]
                    or not source["parent_turn_id"]
                ):
                    return False
                root = connection.execute(
                    """SELECT * FROM turns
                       WHERE session_id=? AND turn_id=?
                         AND classification='NOTIFIABLE_ROOT'""",
                    (source["parent_session_id"], source["parent_turn_id"]),
                ).fetchone()
                if root is None:
                    return False
            elif source["classification"] not in {
                "PENDING_ROOT_CANDIDATE",
                "NOTIFIABLE_ROOT",
            }:
                return False
            if (
                root["lifecycle"] != "RUNNING"
                or root["terminal_status"] is not None
                or not root["notify_pair"]
                or root["suppressed"]
            ):
                return False
            start = self._start_outbox_row(connection, root)
            pending_root = root["classification"] == "PENDING_ROOT_CANDIDATE"
            if (start is None and not pending_root) or (
                start is not None and start["status"] == "suppressed"
            ):
                return False
            operation = _permission_operation(event.tool_name)
            if is_child:
                operation = f"子任务审批：{operation}"
            event_key = _event_key(
                root["session_id"],
                root["turn_id"],
                f"permission:{event.event_fingerprint}",
            )
            cursor = self._insert_outbox(
                connection,
                event_key=event_key,
                event_type="permission",
                session_id=root["session_id"],
                turn_id=root["turn_id"],
                payload={
                    "project": root["project"],
                    "turn_id": root["turn_id"],
                    "event_id": _delivery_id(event_key),
                    "occurred_at": now,
                    "summary": operation,
                    "reason": safe_summary(event.reason, 160),
                    "confirmed": True,
                },
                due_at=(
                    max(now, float(root["decision_due_at"]))
                    if pending_root and root["decision_due_at"] is not None
                    else now
                ),
                now=now,
                depends_on_event_key=_event_key(
                    root["session_id"], root["turn_id"], "started"
                ),
            )
            return cursor.rowcount == 1

    def record_request_user_input(
        self, event: RequestUserInputEvent, *, now: float | None = None
    ) -> bool:
        if (
            event.tool_name != "request_user_input"
            or not _valid_event_identity(
                HookEvent(event.session_id, event.turn_id, "")
            )
            or not _valid_fingerprint(event.signal_fingerprint)
        ):
            return False
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                not self.is_enabled(connection)
                or not self.is_experimental_enabled(
                    "request-user-input", connection
                )
            ):
                return False
            source = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                (event.session_id, event.turn_id),
            ).fetchone()
            if (
                source is None
                or source["lifecycle"] != "RUNNING"
                or source["terminal_status"] is not None
            ):
                return False
            is_child = source["classification"] == "CONFIRMED_CHILD"
            root = source
            if is_child:
                if (
                    source["relation_state"] != "CONFIRMED"
                    or not source["parent_session_id"]
                    or not source["parent_turn_id"]
                ):
                    return False
                root = connection.execute(
                    """SELECT * FROM turns
                       WHERE session_id=? AND turn_id=?
                         AND classification='NOTIFIABLE_ROOT'""",
                    (source["parent_session_id"], source["parent_turn_id"]),
                ).fetchone()
                if root is None:
                    return False
            elif source["classification"] not in {
                "PENDING_ROOT_CANDIDATE",
                "NOTIFIABLE_ROOT",
            }:
                return False
            if (
                root["lifecycle"] != "RUNNING"
                or root["terminal_status"] is not None
                or not root["notify_pair"]
                or root["suppressed"]
            ):
                return False
            start = self._start_outbox_row(connection, root)
            pending_root = root["classification"] == "PENDING_ROOT_CANDIDATE"
            if (start is None and not pending_root) or (
                start is not None and start["status"] == "suppressed"
            ):
                return False
            event_key = _event_key(
                root["session_id"],
                root["turn_id"],
                f"request-user-input:{event.signal_fingerprint}",
            )
            cursor = self._insert_outbox(
                connection,
                event_key=event_key,
                event_type="experimental_request_user_input",
                session_id=root["session_id"],
                turn_id=root["turn_id"],
                payload={
                    **_experimental_payload(
                        event_key,
                        now,
                        "hook",
                        "request_user_input",
                        event.signal_fingerprint[:16],
                    ),
                    "project": root["project"],
                    "turn_id": root["turn_id"],
                    "child_signal": is_child,
                },
                due_at=(
                    max(now, float(root["decision_due_at"]))
                    if pending_root and root["decision_due_at"] is not None
                    else now
                ),
                now=now,
                depends_on_event_key=_event_key(
                    root["session_id"], root["turn_id"], "started"
                ),
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
                if not confirmed_root:
                    connection.execute(
                        """UPDATE outbox
                           SET status='suppressed',
                               last_error='root identity was not confirmed'
                           WHERE session_id=? AND turn_id=?
                             AND event_type IN (
                                 'permission', 'experimental_request_user_input'
                             )
                             AND status IN ('pending', 'retry')""",
                        (turn["session_id"], turn["turn_id"]),
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
        app_thread_id: str | None = None,
        parent_thread_id: str | None,
        source_kind: str,
        now: float | None = None,
    ) -> bool:
        if not _valid_identifier(session_id):
            return False
        if turn_id is not None and not _valid_identifier(turn_id):
            return False
        if app_thread_id is not None and not _valid_identifier(app_thread_id):
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
                app_thread_id,
                session_id,
            ]
            if turn_id is not None:
                parameters.append(turn_id)
            cursor = connection.execute(
                f"""UPDATE turns
                   SET decision_reason=?, classification_source='app_server_metadata',
                       relation_state=?, relation_source='app_server_metadata',
                       app_thread_id=COALESCE(?, app_thread_id)
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
                if existing_completion is None and turn["terminal_status"] in {
                    "completed",
                    "failed",
                    "interrupted",
                }:
                    connection.execute(
                        """UPDATE turns
                           SET pending_completed_at=COALESCE(pending_completed_at, ?),
                               pending_completion_summary=CASE
                                   WHEN ?<>'' THEN ?
                                   ELSE pending_completion_summary
                               END,
                               pending_completion_enabled=MAX(
                                   pending_completion_enabled, ?
                               )
                           WHERE session_id=? AND turn_id=?""",
                        (
                            now,
                            result_summary,
                            result_summary,
                            1 if completion_enabled else 0,
                            turn["session_id"],
                            turn["turn_id"],
                        ),
                    )
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
                       lifecycle='COMPLETED', state='completed',
                       terminal_check_attempts=0, terminal_check_due_at=?,
                       terminal_calibration_deadline=?
                   WHERE session_id=? AND turn_id=?""",
                (
                    now,
                    now,
                    result_summary,
                    1 if completion_enabled else 0,
                    now,
                    now + TERMINAL_CALIBRATION_SECONDS,
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
            return False

    def pending_terminal_turns(
        self, *, now: float | None = None, limit: int = 1
    ) -> list[tuple[str, str, str]]:
        now = now if now is not None else time.time()
        cutoff = now - OUTBOX_RETENTION_SECONDS
        with self.managed_connection() as connection:
            connection.execute(
                """UPDATE turns
                   SET terminal_scan_stopped_at=COALESCE(terminal_scan_stopped_at, ?),
                       terminal_check_due_at=NULL
                   WHERE terminal_status IS NULL AND started_at<=?
                     AND terminal_scan_stopped_at IS NULL""",
                (now, cutoff),
            )
            rows = connection.execute(
                """SELECT session_id, turn_id, app_thread_id FROM turns
                   WHERE classification='NOTIFIABLE_ROOT'
                     AND notify_pair=1 AND suppressed=0
                     AND terminal_status IS NULL
                     AND terminal_scan_stopped_at IS NULL
                     AND app_thread_id IS NOT NULL
                     AND started_at>?
                     AND COALESCE(terminal_check_due_at, started_at)<=?
                     AND EXISTS(
                         SELECT 1 FROM outbox
                         WHERE outbox.session_id=turns.session_id
                           AND outbox.turn_id=turns.turn_id
                           AND outbox.event_type='started'
                           AND outbox.status<>'suppressed'
                     )
                   ORDER BY CASE WHEN pending_completed_at IS NULL THEN 1 ELSE 0 END,
                            COALESCE(terminal_check_due_at, started_at), started_at
                   LIMIT ?""",
                (cutoff, now, max(0, limit)),
            ).fetchall()
            return [
                (row["session_id"], row["turn_id"], row["app_thread_id"])
                for row in rows
            ]

    def record_terminal_probe(
        self,
        session_id: str,
        turn_id: str,
        status: object | None,
        *,
        now: float | None = None,
    ) -> bool:
        if not _valid_identifier(session_id) or not _valid_identifier(turn_id):
            return False
        now = now if now is not None else time.time()
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                (session_id, turn_id),
            ).fetchone()
            if turn is None or turn["suppressed"] or turn["terminal_scan_stopped_at"] is not None:
                return False
            if status is not None and getattr(status, "turn_id", None) != turn_id:
                status = None
            attempts = int(turn["terminal_check_attempts"] or 0) + 1
            self._write_setting(connection, "last_terminal_query_at", str(now), now)
            self._write_setting(
                connection, "last_terminal_query_ok", "1" if status is not None else "0", now
            )
            if status is None:
                delay = TERMINAL_SCAN_RETRY_SECONDS[
                    min(attempts - 1, len(TERMINAL_SCAN_RETRY_SECONDS) - 1)
                ]
                fallback_due = (
                    turn["pending_completed_at"] is not None
                    and turn["terminal_calibration_deadline"] is not None
                    and float(turn["terminal_calibration_deadline"]) <= now
                )
                next_due = now + delay
                if (
                    turn["pending_completed_at"] is not None
                    and turn["terminal_calibration_deadline"] is not None
                ):
                    next_due = min(
                        next_due, float(turn["terminal_calibration_deadline"])
                    )
                connection.execute(
                    """UPDATE turns
                       SET terminal_check_attempts=?, terminal_check_due_at=?
                       WHERE session_id=? AND turn_id=? AND terminal_status IS NULL""",
                    (
                        attempts,
                        None if fallback_due else next_due,
                        session_id,
                        turn_id,
                    ),
                )
                return False
            observed_status = getattr(status, "status", None)
            if observed_status == "inProgress":
                delay = TERMINAL_SCAN_RETRY_SECONDS[
                    min(attempts - 1, len(TERMINAL_SCAN_RETRY_SECONDS) - 1)
                ]
                fallback_due = (
                    turn["pending_completed_at"] is not None
                    and turn["terminal_calibration_deadline"] is not None
                    and float(turn["terminal_calibration_deadline"]) <= now
                )
                next_due = now + delay
                if (
                    turn["pending_completed_at"] is not None
                    and turn["terminal_calibration_deadline"] is not None
                ):
                    next_due = min(
                        next_due, float(turn["terminal_calibration_deadline"])
                    )
                connection.execute(
                    """UPDATE turns
                       SET terminal_check_attempts=?, terminal_check_due_at=?
                       WHERE session_id=? AND turn_id=? AND terminal_status IS NULL""",
                    (
                        attempts,
                        None if fallback_due else next_due,
                        session_id,
                        turn_id,
                    ),
                )
                return False
            if observed_status not in {"completed", "failed", "interrupted"}:
                return False
            existing = turn["terminal_status"]
            if existing is not None:
                if existing != observed_status:
                    self._increment_setting(connection, "terminal_status_conflicts", now)
                return False
            completed_at = getattr(status, "completed_at", None)
            started_at = getattr(status, "started_at", None)
            duration_ms = getattr(status, "duration_ms", None)
            error_category = getattr(status, "error_category", None)
            effective_completed = float(completed_at) if completed_at is not None else now
            connection.execute(
                """UPDATE turns
                   SET terminal_status=?, terminal_source='app_server',
                       terminal_error_category=?, terminal_started_at=?,
                       terminal_completed_at=?, terminal_duration_ms=?,
                       completed_at=COALESCE(completed_at, ?),
                       pending_completed_at=COALESCE(pending_completed_at, ?),
                       lifecycle='COMPLETED', state=?,
                       terminal_check_attempts=?, terminal_check_due_at=NULL,
                       terminal_calibration_deadline=NULL,
                       aggregation_due_at=?
                   WHERE session_id=? AND turn_id=? AND terminal_status IS NULL""",
                (
                    observed_status,
                    error_category,
                    started_at,
                    completed_at,
                    duration_ms,
                    effective_completed,
                    effective_completed,
                    observed_status,
                    attempts,
                    now + FINAL_AGGREGATION_SECONDS,
                    session_id,
                    turn_id,
                ),
            )
            return True

    @staticmethod
    def _increment_setting(
        connection: sqlite3.Connection, key: str, now: float
    ) -> None:
        row = connection.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        try:
            value = int(row["value"]) + 1 if row is not None else 1
        except (TypeError, ValueError):
            value = 1
        NotificationStore._write_setting(connection, key, str(value), now)

    def finalize_aggregations(
        self, *, now: float | None = None, require_due_probe: bool = False
    ) -> int:
        now = now if now is not None else time.time()
        finalized = 0
        with self.managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            probe_gate = (
                " AND (app_thread_id IS NULL OR terminal_check_due_at IS NULL)"
                if require_due_probe
                else ""
            )
            connection.execute(
                f"""UPDATE turns
                   SET terminal_status='completed',
                       terminal_source='agent_turn_complete_fallback',
                       terminal_started_at=started_at,
                       terminal_completed_at=COALESCE(pending_completed_at, completed_at, ?),
                       terminal_duration_ms=MAX(
                           0,
                           CAST((COALESCE(pending_completed_at, completed_at, ?) - started_at)
                                * 1000 AS INTEGER)
                       ),
                       terminal_check_due_at=NULL,
                       terminal_calibration_deadline=NULL,
                       aggregation_due_at=COALESCE(pending_completed_at, completed_at, ?) + ?
                   WHERE classification='NOTIFIABLE_ROOT'
                     AND terminal_status IS NULL
                     AND pending_completed_at IS NOT NULL
                     AND notify_pair=1 AND suppressed=0
                     AND EXISTS(
                         SELECT 1 FROM outbox
                         WHERE outbox.session_id=turns.session_id
                           AND outbox.turn_id=turns.turn_id
                           AND outbox.event_type='started'
                           AND outbox.status<>'suppressed'
                     )
                     {probe_gate}
                     AND terminal_calibration_deadline<=?""",
                (now, now, now, FINAL_AGGREGATION_SECONDS, now),
            )
            roots = connection.execute(
                """SELECT * FROM turns
                   WHERE classification='NOTIFIABLE_ROOT'
                     AND lifecycle='COMPLETED'
                     AND terminal_status IN ('completed', 'failed', 'interrupted')
                     AND notify_pair=1 AND suppressed=0
                     AND aggregation_due_at IS NOT NULL
                     AND aggregation_due_at<=?
                     AND EXISTS(
                         SELECT 1 FROM outbox
                         WHERE outbox.session_id=turns.session_id
                           AND outbox.turn_id=turns.turn_id
                           AND outbox.event_type='started'
                           AND outbox.status<>'suppressed'
                     )
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
                    float(root["terminal_completed_at"] or root["completed_at"] or now),
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
                "started_at": turn["terminal_started_at"] or turn["started_at"],
                "duration_seconds": (
                    max(0, int(turn["terminal_duration_ms"] / 1000))
                    if turn["terminal_duration_ms"] is not None
                    else max(0, int(completed_at - turn["started_at"]))
                ),
                "summary": result_summary,
                "terminal_status": turn["terminal_status"] or "completed",
                "error_category": turn["terminal_error_category"],
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
        self,
        *,
        limit: int,
        now: float | None = None,
        dependent_only: bool = False,
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
            dependent_filter = (
                " AND depends_on_event_key IS NOT NULL" if dependent_only else ""
            )
            rows = connection.execute(
                f"""SELECT * FROM outbox
                   WHERE status IN ('pending', 'retry') AND next_attempt_at<=?
                     {dependent_filter}
                     AND (
                         event_type NOT IN (
                             'permission', 'experimental_request_user_input'
                         )
                         OR EXISTS(
                             SELECT 1 FROM turns
                             WHERE turns.session_id=outbox.session_id
                               AND turns.turn_id=outbox.turn_id
                               AND turns.classification='NOTIFIABLE_ROOT'))
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
            terminal_counts = {
                row["terminal_status"]: row["count"]
                for row in connection.execute(
                    """SELECT terminal_status, COUNT(*) AS count FROM turns
                       WHERE terminal_status IN ('completed', 'failed', 'interrupted')
                         AND classification='NOTIFIABLE_ROOT'
                       GROUP BY terminal_status"""
                )
            }
            waiting_terminal = connection.execute(
                """SELECT COUNT(*) AS count FROM turns
                   WHERE classification='NOTIFIABLE_ROOT'
                     AND notify_pair=1 AND suppressed=0
                     AND terminal_status IS NULL
                     AND terminal_scan_stopped_at IS NULL
                     AND started_at>?""",
                (now - OUTBOX_RETENTION_SECONDS,),
            ).fetchone()["count"]
            permission_total = connection.execute(
                "SELECT COUNT(*) AS count FROM outbox WHERE event_type='permission'"
            ).fetchone()["count"]
            permission_sent = connection.execute(
                """SELECT COUNT(*) AS count FROM outbox
                   WHERE event_type='permission' AND status='sent'"""
            ).fetchone()["count"]
            best_effort_total = connection.execute(
                """SELECT COUNT(*) AS count FROM outbox
                   WHERE event_type LIKE 'experimental_%'"""
            ).fetchone()["count"]
            best_effort_sent = connection.execute(
                """SELECT COUNT(*) AS count FROM outbox
                   WHERE event_type LIKE 'experimental_%' AND status='sent'"""
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
                "waiting_terminal": waiting_terminal,
                "completed_turns": terminal_counts.get("completed", 0),
                "failed_turns": terminal_counts.get("failed", 0),
                "interrupted_turns": terminal_counts.get("interrupted", 0),
                "permission_total": permission_total,
                "permission_sent": permission_sent,
                "confirmed_total": connection.execute(
                    """SELECT COUNT(*) AS count FROM outbox
                       WHERE event_type IN ('started', 'completed', 'permission')"""
                ).fetchone()["count"],
                "best_effort_total": best_effort_total,
                "best_effort_sent": best_effort_sent,
                "experimental_features": self.experimental_feature_status(),
                "last_experimental_query_at": settings.get(
                    "experimental.query.last_success_at"
                ),
                "last_terminal_query_ok": settings.get("last_terminal_query_ok"),
                "last_terminal_query_at": settings.get("last_terminal_query_at"),
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


def _valid_permission_event(event: PermissionEvent) -> bool:
    return (
        _valid_identifier(event.session_id)
        and _valid_identifier(event.turn_id)
        and _valid_fingerprint(event.event_fingerprint)
    )


def _valid_fingerprint(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_experimental_feature(feature: str) -> None:
    if feature not in EXPERIMENTAL_FEATURES:
        raise ValueError(f"未知实验功能：{feature}")


def _experimental_payload(
    event_key: str,
    observed_at: float,
    signal_source: str,
    signal_kind: str,
    signal_id: str,
    *,
    display_name: str = "",
) -> dict[str, Any]:
    return {
        "certainty": "best_effort",
        "signal_source": signal_source,
        "signal_kind": signal_kind,
        "observed_at": observed_at,
        "occurred_at": observed_at,
        "signal_id": signal_id,
        "event_id": _delivery_id(event_key),
        "display_name": safe_summary(display_name, 80),
    }


def _permission_operation(tool_name: str) -> str:
    normalized = "".join(character for character in tool_name.lower() if character.isalnum())
    if any(marker in normalized for marker in ("shell", "command", "exec", "bash")):
        return "命令执行审批"
    if any(marker in normalized for marker in ("applypatch", "filewrite", "edit", "write")):
        return "文件变更审批"
    if any(marker in normalized for marker in ("network", "websearch", "web", "http")):
        return "网络访问审批"
    if "mcp" in normalized:
        return "MCP 工具审批"
    return "工具权限审批"
