import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
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

    def _confirm_root(self, session="session", *, now=15):
        self.store.record_thread_metadata(
            session, parent_thread_id=None, source_kind="appServer", now=now
        )

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

        self._confirm_root()
        self.store.finalize_pending(now=15)
        self.store.finalize_aggregations(now=17)
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
        self.assertEqual(self._turn("agent", "child-turn")["classification"], "UNVERIFIED")

    def test_late_relation_never_retracts_or_suppresses_start(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("child-session", "turn", "", prompt="work"), now=10)
        self._confirm_root("child-session")
        self.store.finalize_pending(now=15)
        start = self.store.claim_due(limit=1, now=15)[0]
        self.store.mark_sent(start["id"], now=15)
        self.store.record_subagent_start(
            SubagentEvent("child-session", "worker", "parent", "parent-turn"),
            now=16,
        )
        self.assertFalse(
            self.store.record_completion(
                HookEvent("child-session", "turn", "", last_assistant_message="done"),
                now=17,
            )
        )
        self.store.finalize_aggregations(now=22)
        self.assertEqual(
            [row["event_type"] for row in self._events()],
            ["started", "completed"],
        )

    def test_late_exact_child_relation_preserves_already_queued_root_pair(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(
            HookEvent("parent", "parent-turn", "", prompt="parent"), now=5
        )
        self.store.record_start(
            HookEvent("candidate", "candidate-turn", "", prompt="candidate"),
            now=10,
        )
        self._confirm_root("candidate")
        self.store.finalize_pending(now=15)
        self.store.record_subagent_start(
            SubagentEvent(
                "candidate",
                "worker",
                "parent",
                "candidate-turn",
            ),
            now=16,
        )

        self.assertFalse(
            self.store.record_completion(
                HookEvent(
                    "candidate",
                    "candidate-turn",
                    "",
                    last_assistant_message="done",
                ),
                now=17,
            )
        )
        self.store.finalize_aggregations(now=22)

        self.assertEqual(
            [row["event_type"] for row in self._events()],
            ["started", "completed"],
        )
        self.assertEqual(
            self._turn("candidate", "candidate-turn")["classification"],
            "NOTIFIABLE_ROOT",
        )

    def test_missing_start_completion_is_silent_and_idempotent(self):
        self.store.set_enabled(True, now=1)
        completion = HookEvent("session", "turn", "", last_assistant_message="done")
        self.assertFalse(self.store.record_completion(completion, now=10))
        self.assertFalse(self.store.record_completion(completion, now=11))
        self.assertEqual(self._events(), [])

    def test_start_when_off_then_completion_after_on_is_silent(self):
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self.store.finalize_pending(now=15)
        self.assertEqual(self._events(), [])

    def test_confirmed_root_started_while_off_never_gets_completion_only_notice(self):
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self.store.set_enabled(True, now=11)
        self._confirm_root(now=14)
        self.store.finalize_pending(now=15)
        self.store.record_completion(
            HookEvent("session", "turn", "", last_assistant_message="done"),
            now=16,
        )

        self.store.finalize_aggregations(now=100)
        self.assertEqual(self._events(), [])
        self.store.set_enabled(True, now=16)
        self.assertFalse(
            self.store.record_completion(
                HookEvent("session", "turn", "", last_assistant_message="done"),
                now=17,
            )
        )
        self.assertEqual(self._events(), [])

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
        self._confirm_root()
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
        self._confirm_root()
        self.store.finalize_pending(now=15)

        self.assertEqual(self._events(), [])
        turn = self._turn()
        self.assertEqual(turn["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual(turn["lifecycle"], "COMPLETED")

    def test_graceful_off_and_finalization_are_serializable(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(HookEvent("session", "turn", "", prompt="work"), now=10)
        self._confirm_root()
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
        self.assertIn([row["event_type"] for row in self._events()], ([], ["started"]))
        self.assertIn(
            self._turn()["classification"],
            ("NOTIFIABLE_ROOT", "CONFIRMED_CHILD", "UNVERIFIED"),
        )

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
        self.assertEqual(version, 10)
        self.assertEqual(turn["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual(turn["lifecycle"], "COMPLETED")
        self.assertEqual(turn["terminal_status"], "completed")
        self.assertEqual(pending["classification"], "UNVERIFIED")
        self.assertEqual(pending["lifecycle"], "COMPLETED")
        self.assertEqual(pending["state"], "completed")
        self.assertEqual(pending["completed_at"], 12)
        self.assertIsNotNone(legacy_table)
        with legacy_store.managed_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        self.assertEqual(legacy_store.finalize_pending(now=100), 0)
        with legacy_store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT session_id, event_type FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("old-session", "started"),
            ],
        )

    def test_v5_fail_open_rows_are_suppressed_but_sent_start_keeps_pair(self):
        root = Path(self.temp_dir.name) / "v5"
        paths = AppPaths(root)
        original_store = NotificationStore(paths)
        with original_store.managed_connection() as connection:
            connection.execute("PRAGMA user_version=5")
            for session_id in ("unsent", "sent-start"):
                connection.execute(
                    """INSERT INTO turns(
                           session_id, turn_id, cwd, project, prompt_summary,
                           started_at, completed_at, notify_pair, suppressed,
                           state, classification, lifecycle, decision_reason,
                           pending_completed_at, pending_completion_summary,
                           pending_completion_enabled
                       ) VALUES(?, 'turn', '', 'demo', 'work', 10, 20, 1, 0,
                                'completed', 'NOTIFIABLE_ROOT', 'COMPLETED',
                                'public_contract_fail_open', 20, 'done', 1)""",
                    (session_id,),
                )
            connection.execute(
                """INSERT INTO turns(
                       session_id, turn_id, cwd, project, prompt_summary,
                       started_at, notify_pair, suppressed, state,
                       classification, lifecycle, decision_reason,
                       classification_source
                   ) VALUES('legacy-empty', 'turn', '', 'demo', 'work',
                            10, 1, 0, 'running', 'NOTIFIABLE_ROOT', 'RUNNING',
                            '', 'public_contract')"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, next_attempt_at, created_at
                   ) VALUES('unsent-start', 'unsent', 'turn', 'started', '{}',
                            'pending', 10, 10)"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, next_attempt_at, created_at
                   ) VALUES('unsent-complete', 'unsent', 'turn', 'completed', '{}',
                            'sending', 10, 10)"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, next_attempt_at, created_at
                   ) VALUES('legacy-empty-start', 'legacy-empty', 'turn',
                            'started', '{}', 'pending', 10, 10)"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, next_attempt_at, created_at, sent_at
                   ) VALUES('sent-start', 'sent-start', 'turn', 'started', '{}',
                            'sent', 10, 10, 11)"""
            )

        migrated = NotificationStore(paths)
        with migrated.managed_connection() as connection:
            unsent_turn = connection.execute(
                "SELECT classification FROM turns WHERE session_id='unsent'"
            ).fetchone()[0]
            unsent_outbox = connection.execute(
                "SELECT event_key, status FROM outbox WHERE session_id='unsent' "
                "ORDER BY event_key"
            ).fetchall()
            legacy_empty = connection.execute(
                "SELECT classification, decision_reason FROM turns "
                "WHERE session_id='legacy-empty'"
            ).fetchone()
            legacy_empty_status = connection.execute(
                "SELECT status FROM outbox WHERE session_id='legacy-empty'"
            ).fetchone()[0]
            paired_turn = connection.execute(
                "SELECT decision_reason, aggregation_due_at FROM turns "
                "WHERE session_id='sent-start'"
            ).fetchone()
        self.assertEqual(unsent_turn, "UNVERIFIED")
        self.assertEqual(
            [(row["event_key"], row["status"]) for row in unsent_outbox],
            [
                ("unsent-complete", "suppressed"),
                ("unsent-start", "suppressed"),
            ],
        )
        self.assertEqual(legacy_empty["classification"], "UNVERIFIED")
        self.assertEqual(legacy_empty["decision_reason"], "legacy_fail_open_suppressed")
        self.assertEqual(legacy_empty_status, "suppressed")
        self.assertEqual(paired_turn["decision_reason"], "legacy_sent_start_pair")
        self.assertIsNotNone(paired_turn["aggregation_due_at"])

        self.assertEqual(migrated.finalize_aggregations(now=100), 1)
        with migrated.managed_connection() as connection:
            completions = connection.execute(
                "SELECT session_id FROM outbox WHERE event_type='completed' "
                "AND status<>'suppressed'"
            ).fetchall()
        self.assertEqual([row[0] for row in completions], ["sent-start"])

    def test_migration_does_not_downgrade_a_newer_schema_marker(self):
        root = Path(self.temp_dir.name) / "newer"
        paths = AppPaths(root)
        store = NotificationStore(paths)
        with store.managed_connection() as connection:
            connection.execute("PRAGMA user_version=99")

        with NotificationStore(paths).managed_connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 99)

    def test_v8_migration_silences_unobservable_legacy_running_roots(self):
        root = Path(self.temp_dir.name) / "v8-running"
        paths = AppPaths(root)
        store = NotificationStore(paths)
        with store.managed_connection() as connection:
            connection.execute("PRAGMA user_version=8")
            connection.execute(
                """INSERT INTO turns(
                       session_id, turn_id, cwd, project, prompt_summary,
                       started_at, notify_pair, suppressed, state,
                       classification, lifecycle, decision_reason,
                       app_thread_id, terminal_status
                   ) VALUES('legacy-running', 'turn', '', 'demo', 'work',
                            10, 1, 0, 'running', 'NOTIFIABLE_ROOT', 'RUNNING',
                            'legacy_sent_start_pair', NULL, NULL)"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, attempts, next_attempt_at, created_at, sent_at
                   ) VALUES('legacy-start', 'legacy-running', 'turn', 'started',
                            '{}', 'sent', 1, 10, 10, 11)"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, attempts, next_attempt_at, created_at
                   ) VALUES('legacy-interrupted', 'legacy-running', 'turn',
                            'completed', '{"terminal_status":"interrupted"}',
                            'pending', 0, 12, 12)"""
            )

        migrated = NotificationStore(paths)
        with migrated.managed_connection() as connection:
            turn = connection.execute(
                "SELECT * FROM turns WHERE session_id='legacy-running'"
            ).fetchone()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            outbox_rows = connection.execute(
                "SELECT event_type, status FROM outbox ORDER BY id"
            ).fetchall()

        self.assertEqual(version, 10)
        self.assertEqual(turn["classification"], "UNVERIFIED")
        self.assertEqual(turn["lifecycle"], "COMPLETED")
        self.assertEqual(turn["state"], "unverified")
        self.assertEqual(turn["suppressed"], 1)
        self.assertEqual(turn["notify_pair"], 0)
        self.assertEqual(
            turn["suppression_reason"], "legacy_missing_app_thread_id"
        )
        self.assertIsNotNone(turn["terminal_scan_stopped_at"])
        self.assertEqual(
            [tuple(row) for row in outbox_rows],
            [("started", "sent")],
        )

    def test_v8_migration_reconciles_unreliable_interrupted_observations(self):
        root = Path(self.temp_dir.name) / "v8-interrupted"
        paths = AppPaths(root)
        store = NotificationStore(paths)
        with store.managed_connection() as connection:
            connection.execute("PRAGMA user_version=8")
            for session_id, completion_status in (
                ("already-sent", "sent"),
                ("not-sent", "pending"),
                ("dead-unsent", "dead"),
                ("user-suppressed", "suppressed"),
            ):
                connection.execute(
                    """INSERT INTO turns(
                           session_id, turn_id, cwd, project, prompt_summary,
                           started_at, completed_at, notify_pair, suppressed,
                           state, classification, lifecycle, decision_reason,
                           app_thread_id, terminal_status, terminal_source,
                           terminal_started_at, terminal_completed_at,
                           terminal_duration_ms, pending_completed_at,
                           pending_completion_enabled, aggregation_due_at
                       ) VALUES(?, 'turn', '', 'demo', 'work', 10, 12, 1, 0,
                                'interrupted', 'NOTIFIABLE_ROOT', 'COMPLETED',
                                'metadata_confirmed_root', ?, 'interrupted',
                                'app_server', 10, NULL, NULL, 12, NULL, 17)""",
                    (session_id, f"thread-{session_id}"),
                )
                connection.execute(
                    """INSERT INTO outbox(
                           event_key, session_id, turn_id, event_type,
                           payload_json, status, attempts, next_attempt_at,
                           created_at, sent_at
                       ) VALUES(?, ?, 'turn', 'completed',
                                '{"terminal_status":"interrupted"}', ?, 1,
                                12, 12, ?)""",
                    (
                        f"completion-{session_id}",
                        session_id,
                        completion_status,
                        13 if completion_status == "sent" else None,
                    ),
                )
            connection.execute(
                """INSERT INTO turns(
                       session_id, turn_id, cwd, project, prompt_summary,
                       started_at, completed_at, notify_pair, suppressed,
                       state, classification, lifecycle, decision_reason,
                       app_thread_id, terminal_status, terminal_source,
                       terminal_started_at, terminal_completed_at,
                       terminal_duration_ms, pending_completed_at,
                       pending_completion_summary, pending_completion_enabled,
                       aggregation_due_at
                   ) VALUES('hook-completed', 'turn', '', 'demo', 'work',
                            10, 11, 1, 0, 'interrupted', 'NOTIFIABLE_ROOT',
                            'COMPLETED', 'metadata_confirmed_root',
                            'thread-hook-completed', 'interrupted', 'app_server',
                            10, 12, 2000, 11, 'authoritative result', 1, 17)"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, attempts, next_attempt_at, created_at, sent_at
                   ) VALUES('start-hook-completed', 'hook-completed', 'turn',
                            'started', '{}', 'sent', 1, 10, 10, 10)"""
            )
            connection.execute(
                """INSERT INTO outbox(
                       event_key, session_id, turn_id, event_type, payload_json,
                       status, attempts, next_attempt_at, created_at
                   ) VALUES('completion-hook-completed', 'hook-completed', 'turn',
                            'completed', '{"terminal_status":"interrupted"}',
                            'pending', 0, 12, 12)"""
            )

        migrated = NotificationStore(paths)
        with migrated.managed_connection() as connection:
            sent = connection.execute(
                "SELECT * FROM turns WHERE session_id='already-sent'"
            ).fetchone()
            unsent = connection.execute(
                "SELECT * FROM turns WHERE session_id='not-sent'"
            ).fetchone()
            dead = connection.execute(
                "SELECT * FROM turns WHERE session_id='dead-unsent'"
            ).fetchone()
            suppressed = connection.execute(
                "SELECT * FROM turns WHERE session_id='user-suppressed'"
            ).fetchone()
            hook_completed = connection.execute(
                "SELECT * FROM turns WHERE session_id='hook-completed'"
            ).fetchone()
            outbox = connection.execute(
                "SELECT session_id, status FROM outbox ORDER BY id"
            ).fetchall()

        self.assertEqual(sent["classification"], "UNVERIFIED")
        self.assertEqual(sent["lifecycle"], "COMPLETED")
        self.assertEqual(sent["state"], "unverified")
        self.assertEqual(sent["suppressed"], 1)
        self.assertIsNone(sent["terminal_status"])
        self.assertEqual(
            sent["suppression_reason"], "legacy_unreliable_interrupted_sent"
        )
        self.assertEqual(unsent["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual(unsent["lifecycle"], "RUNNING")
        self.assertEqual(unsent["state"], "running")
        self.assertIsNone(unsent["terminal_status"])
        self.assertIsNone(unsent["pending_completed_at"])
        self.assertIsNone(unsent["pending_completion_enabled"])
        self.assertIsNotNone(unsent["terminal_check_due_at"])
        self.assertEqual(dead["lifecycle"], "RUNNING")
        self.assertIsNone(dead["terminal_status"])
        self.assertIsNone(dead["pending_completed_at"])
        self.assertEqual(suppressed["classification"], "UNVERIFIED")
        self.assertEqual(suppressed["lifecycle"], "COMPLETED")
        self.assertEqual(suppressed["suppressed"], 1)
        self.assertEqual(
            suppressed["suppression_reason"],
            "legacy_unreliable_interrupted_suppressed",
        )
        self.assertEqual(hook_completed["lifecycle"], "COMPLETED")
        self.assertEqual(hook_completed["state"], "completed")
        self.assertEqual(hook_completed["pending_completed_at"], 11)
        self.assertEqual(
            hook_completed["pending_completion_summary"], "authoritative result"
        )
        self.assertIsNone(hook_completed["terminal_status"])
        self.assertIsNotNone(hook_completed["terminal_check_due_at"])
        self.assertIsNotNone(hook_completed["terminal_calibration_deadline"])
        self.assertEqual(
            [tuple(row) for row in outbox],
            [
                ("already-sent", "sent"),
                ("user-suppressed", "suppressed"),
                ("hook-completed", "sent"),
            ],
        )

        migrated.finalize_aggregations(now=2_000_000_000)
        with migrated.managed_connection() as connection:
            recovered = connection.execute(
                """SELECT payload_json FROM outbox
                   WHERE session_id='hook-completed' AND event_type='completed'"""
            ).fetchone()
        self.assertEqual(json.loads(recovered["payload_json"])["terminal_status"], "completed")

    def test_v9_migration_suppresses_all_unsent_permission_notifications(self):
        root = Path(self.temp_dir.name) / "v9-permissions"
        paths = AppPaths(root)
        store = NotificationStore(paths)
        with store.managed_connection() as connection:
            connection.execute("PRAGMA user_version=9")
            for index, status in enumerate(
                ("pending", "retry", "sending", "dead", "sent", "suppressed")
            ):
                connection.execute(
                    """INSERT INTO outbox(
                           event_key, event_type, payload_json, status, attempts,
                           next_attempt_at, created_at, sent_at, last_error,
                           last_error_at
                       ) VALUES(?, 'permission', '{}', ?, 1, 10, 10, ?, ?, 9)""",
                    (
                        f"permission-{status}",
                        status,
                        11 if status == "sent" else None,
                        "old failure" if status in {"retry", "dead"} else None,
                    ),
                )

        migrated = NotificationStore(paths)
        with migrated.managed_connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            rows = connection.execute(
                """SELECT event_key, status, last_error, last_error_at
                   FROM outbox ORDER BY id"""
            ).fetchall()

        self.assertEqual(version, 10)
        for row in rows[:4]:
            self.assertEqual(row["status"], "suppressed")
            self.assertEqual(
                row["last_error"],
                "suppressed by v10 unreliable permission policy",
            )
            self.assertIsNone(row["last_error_at"])
        self.assertEqual(rows[4]["status"], "sent")
        self.assertEqual(rows[5]["status"], "suppressed")

    def test_v6_migration_uses_delivery_file_lock(self):
        root = Path(self.temp_dir.name) / "migration-lock"
        paths = AppPaths(root)
        store = NotificationStore(paths)
        with store.managed_connection() as connection:
            connection.execute("PRAGMA user_version=5")

        class RecordingStore(NotificationStore):
            lock_entries = 0

            @contextmanager
            def _delivery_file_lock(self):
                self.lock_entries += 1
                with super()._delivery_file_lock():
                    yield

        recording_store = RecordingStore(paths)
        with recording_store.managed_connection():
            pass
        self.assertEqual(recording_store.lock_entries, 1)


if __name__ == "__main__":
    unittest.main()
