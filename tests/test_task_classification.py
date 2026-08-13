import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from codex_notify.db import HookEvent, NotificationStore, SubagentEvent
from codex_notify.paths import AppPaths


class TurnStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.store = NotificationStore(self.paths)

    def _turn(self, session="session", turn="turn"):
        with self.store.managed_connection() as connection:
            return connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                (session, turn),
            ).fetchone()

    def _events(self):
        with self.store.managed_connection() as connection:
            return connection.execute("SELECT * FROM outbox ORDER BY id").fetchall()

    def test_completion_during_window_changes_lifecycle_not_classification(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self.assertFalse(
            self.store.record_completion(
                HookEvent("session", "turn", "", last_assistant_message="done"),
                now=12,
            )
        )
        pending = self._turn()
        self.assertEqual(pending["classification"], "PENDING_ROOT_CANDIDATE")
        self.assertEqual(pending["lifecycle"], "COMPLETED")
        self.assertEqual(self._events(), [])

        self.store.finalize_pending(now=15)
        final = self._turn()
        self.assertEqual(final["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual(final["lifecycle"], "COMPLETED")
        self.assertEqual(
            [row["event_type"] for row in self._events()],
            ["started", "completed"],
        )

    def test_multilevel_public_relations_are_stored_edge_by_edge_only(self):
        first = SubagentEvent("agent-a", "worker", "root-session", "root-turn")
        second = SubagentEvent("agent-b", "worker", "agent-a-session", "child-turn")
        self.assertTrue(self.store.record_subagent_start(first, now=1))
        self.assertTrue(self.store.record_subagent_start(second, now=2))
        with self.store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT agent_id, parent_session_id, parent_turn_id "
                "FROM subagents ORDER BY started_at"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("agent-a", "root-session", "root-turn"),
                ("agent-b", "agent-a-session", "child-turn"),
            ],
        )

    def test_relation_conflict_is_recorded_without_polluting_turn_ownership(self):
        first = SubagentEvent("agent", "worker", "parent-a", "turn-a")
        second = SubagentEvent("agent", "worker", "parent-b", "turn-b")
        self.store.record_subagent_start(first, now=1)
        self.assertFalse(self.store.record_subagent_start(second, now=2))
        self.store.set_enabled(True, now=3)
        self.store.record_start(HookEvent("agent", "child-turn", "", prompt="work"), now=4)
        self.store.finalize_pending(now=9)
        with self.store.managed_connection() as connection:
            relation = connection.execute("SELECT state FROM subagents").fetchone()[0]
        self.assertEqual(relation, "conflict")
        self.assertEqual(self._turn("agent", "child-turn")["classification"], "NOTIFIABLE_ROOT")

    def test_late_relation_never_retracts_or_suppresses_start(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("child-session", "turn", "", prompt="work"), now=10)
        self.store.finalize_pending(now=15)
        start = self.store.claim_due(limit=1, now=15)[0]
        self.store.mark_sent(start["id"], now=15)
        self.store.record_subagent_start(
            SubagentEvent("child-session", "worker", "parent", "parent-turn"),
            now=16,
        )
        self.assertTrue(
            self.store.record_completion(
                HookEvent("child-session", "turn", "", last_assistant_message="done"),
                now=17,
            )
        )
        self.assertEqual(
            [row["event_type"] for row in self._events()],
            ["started", "completed"],
        )

    def test_missing_start_completion_is_standalone_and_idempotent(self):
        self.store.set_enabled(True, now=1)
        completion = HookEvent("session", "turn", "", last_assistant_message="done")
        self.assertTrue(self.store.record_completion(completion, now=10))
        self.assertTrue(self.store.record_completion(completion, now=11))
        rows = self._events()
        self.assertEqual([row["event_type"] for row in rows], ["completed"])
        self.assertTrue(json.loads(rows[0]["payload_json"])["incomplete_lifecycle"])

    def test_start_when_off_then_completion_after_on_is_standalone(self):
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self.store.finalize_pending(now=15)
        self.assertEqual(self._events(), [])
        self.store.set_enabled(True, now=16)
        self.assertTrue(
            self.store.record_completion(
                HookEvent("session", "turn", "", last_assistant_message="done"),
                now=17,
            )
        )
        self.assertEqual([row["event_type"] for row in self._events()], ["completed"])

    def test_completion_that_arrived_while_off_is_not_reconsidered(self):
        completion = HookEvent("session", "turn", "", last_assistant_message="done")
        self.assertFalse(self.store.record_completion(completion, now=10))
        self.store.set_enabled(True, now=11)
        self.assertFalse(self.store.record_completion(completion, now=12))
        self.assertEqual(self._events(), [])

    def test_off_now_suppresses_pending_candidate_permanently(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self.store.set_enabled(False, immediate=True, now=11)
        self.store.set_enabled(True, now=12)
        self.store.finalize_pending(now=15)
        self.assertFalse(
            self.store.record_completion(
                HookEvent("session", "turn", "", last_assistant_message="done"),
                now=16,
            )
        )
        turn = self._turn()
        self.assertEqual(turn["suppressed"], 1)
        self.assertEqual(turn["suppression_reason"], "suppressed by off --now")
        self.assertEqual(self._events(), [])

    def test_graceful_off_cancels_pending_start_without_suppressing_turn(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self.store.set_enabled(False, now=11)
        self.store.finalize_pending(now=15)
        turn = self._turn()
        self.assertEqual(turn["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual(turn["suppressed"], 0)
        self.assertEqual(self._events(), [])

    def test_graceful_off_cancels_completion_waiting_for_finalization(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self.store.record_completion(
            HookEvent("session", "turn", "", last_assistant_message="done"),
            now=12,
        )

        self.store.set_enabled(False, now=13)
        self.store.finalize_pending(now=15)

        self.assertEqual(self._events(), [])
        turn = self._turn()
        self.assertEqual(turn["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual(turn["lifecycle"], "COMPLETED")

    def test_graceful_off_and_finalization_are_serializable(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        barrier = threading.Barrier(2)
        errors = []

        def finalize():
            try:
                barrier.wait(timeout=5)
                NotificationStore(self.paths).finalize_pending(now=15)
            except Exception as exc:
                errors.append(exc)

        def turn_off():
            try:
                barrier.wait(timeout=5)
                NotificationStore(self.paths).set_enabled(False, now=15)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=finalize), threading.Thread(target=turn_off)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertFalse(self.store.is_enabled())
        self.assertIn(
            [row["event_type"] for row in self._events()],
            ([], ["started"]),
        )

    def test_pending_finalization_and_relation_write_are_serialized(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        barrier = threading.Barrier(2)
        errors = []

        def finalize():
            try:
                barrier.wait(timeout=5)
                NotificationStore(self.paths).finalize_pending(now=15)
            except Exception as exc:
                errors.append(exc)

        def relate():
            try:
                barrier.wait(timeout=5)
                NotificationStore(self.paths).record_subagent_start(
                    SubagentEvent("session", "worker", "parent", "parent-turn"),
                    now=15,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=finalize), threading.Thread(target=relate)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual([row["event_type"] for row in self._events()], ["started"])
        self.assertEqual(self._turn()["classification"], "NOTIFIABLE_ROOT")

    def test_invalid_or_unsafe_identity_is_silent(self):
        self.store.set_enabled(True, now=1)
        for session, turn in (("", "turn"), ("session", ""), ("bad\0id", "turn")):
            self.assertFalse(
                self.store.record_start(HookEvent(session, turn, "", prompt="work"), now=10)
            )
            self.assertFalse(
                self.store.record_completion(
                    HookEvent(session, turn, "", last_assistant_message="done"), now=11
                )
            )
        self.assertEqual(self._events(), [])

    def test_legacy_database_migrates_additively_without_replaying_history(self):
        root = Path(self.temp_dir.name) / "legacy"
        paths = AppPaths(root)
        paths.ensure_runtime_dirs()
        with closing(sqlite3.connect(paths.database)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE turns(
                    session_id TEXT NOT NULL, turn_id TEXT NOT NULL, cwd TEXT NOT NULL,
                    project TEXT NOT NULL, prompt_summary TEXT NOT NULL,
                    started_at REAL NOT NULL, completed_at REAL,
                    notify_pair INTEGER NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, classification TEXT NOT NULL DEFAULT 'UNKNOWN',
                    decision_reason TEXT NOT NULL DEFAULT '', decision_due_at REAL,
                    pending_completed_at REAL,
                    pending_completion_summary TEXT NOT NULL DEFAULT '',
                    pending_completion_enabled INTEGER,
                    classification_source TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(session_id, turn_id)
                );
                CREATE TABLE outbox(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
                    session_id TEXT, turn_id TEXT, event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
                    created_at REAL NOT NULL, sent_at REAL, last_error TEXT
                );
                CREATE TABLE managed_launchers(launcher_id TEXT PRIMARY KEY);
                INSERT INTO turns(
                    session_id, turn_id, cwd, project, prompt_summary, started_at,
                    completed_at, notify_pair, suppressed, state, classification
                ) VALUES(
                    'old-session', 'old-turn', '', 'demo', '', 1, 2, 1, 0,
                    'completed', 'USER_ROOT'
                );
                INSERT INTO turns(
                    session_id, turn_id, cwd, project, prompt_summary, started_at,
                    completed_at, notify_pair, suppressed, state, classification,
                    decision_reason, decision_due_at, pending_completed_at,
                    pending_completion_summary, pending_completion_enabled,
                    classification_source
                ) VALUES(
                    'pending-session', 'pending-turn', '', 'demo', 'work', 10,
                    NULL, 1, 0, 'running', 'PENDING', 'awaiting_relation', 15, 12,
                    'done', 1, 'prompt_classifier'
                );
                INSERT INTO outbox(
                    event_key, session_id, turn_id, event_type, payload_json,
                    status, next_attempt_at, created_at, sent_at
                ) VALUES('legacy-start', 'old-session', 'old-turn', 'started', '{}',
                         'sent', 1, 1, 1);
                PRAGMA user_version=4;
                """
            )

        legacy_store = NotificationStore(paths)
        with legacy_store.managed_connection() as connection:
            turn = connection.execute(
                "SELECT * FROM turns WHERE session_id='old-session'"
            ).fetchone()
            pending = connection.execute(
                "SELECT * FROM turns WHERE session_id='pending-session'"
            ).fetchone()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            legacy_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE name='managed_launchers'"
            ).fetchone()
        self.assertEqual(version, 5)
        self.assertEqual(turn["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual(turn["lifecycle"], "COMPLETED")
        self.assertEqual(pending["classification"], "PENDING_ROOT_CANDIDATE")
        self.assertEqual(pending["lifecycle"], "COMPLETED")
        self.assertEqual(pending["state"], "completed")
        self.assertEqual(pending["completed_at"], 12)
        self.assertIsNotNone(legacy_table)
        with legacy_store.managed_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        self.assertEqual(legacy_store.finalize_pending(now=100), 1)
        with legacy_store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT session_id, event_type FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("old-session", "started"),
                ("pending-session", "started"),
                ("pending-session", "completed"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
