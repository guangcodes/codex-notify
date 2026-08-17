import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from codex_notify.constants import OUTBOX_RETENTION_SECONDS
from codex_notify.app_server_status import TerminalStatus
from codex_notify.db import (
    HookEvent,
    NotificationStore,
    PermissionEvent,
    RequestUserInputEvent,
    SubagentEvent,
)
from codex_notify.paths import AppPaths


class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.store = NotificationStore(self.paths)
        self.event = HookEvent("session-1", "turn-1", "/work/example", prompt="work")

    def _start(self, *, now=100, finalize=True):
        self.store.record_start(self.event, now=now)
        if finalize:
            self.store.record_thread_metadata(
                self.event.session_id,
                parent_thread_id=None,
                source_kind="appServer",
                now=now + 5,
            )
            self.store.finalize_pending(now=now + 5)

    def _finish_aggregation(self, *, now):
        self.store.finalize_aggregations(now=now)

    def test_default_is_off_and_disabled_turn_never_gets_completion(self):
        self.assertFalse(self.store.is_enabled())
        self._start()
        self.assertFalse(
            self.store.record_completion(
                HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
                now=110,
            )
        )
        self.assertEqual(self.store.status_snapshot()["pending"], 0)

    def test_enabled_completion_without_start_is_silent(self):
        self.store.set_enabled(True, now=1)
        self.assertFalse(
            self.store.record_completion(
                HookEvent("missing", "turn", "", last_assistant_message="done"),
                now=10,
            )
        )
        self.assertEqual(self.store.claim_due(limit=10, now=10), [])

    def test_enabled_turn_queues_exactly_one_ordered_pair(self):
        self.store.set_enabled(True, now=1)
        self._start()
        self._start(now=101)
        completion = HookEvent("session-1", "turn-1", "", last_assistant_message="done")
        self.assertFalse(self.store.record_completion(completion, now=120))
        self.assertFalse(self.store.record_completion(completion, now=121))
        self._finish_aggregation(now=125)
        with self.store.managed_connection() as connection:
            rows = connection.execute("SELECT event_type FROM outbox ORDER BY id").fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["started", "completed"])
        first = self.store.claim_due(limit=10, now=120)
        self.assertEqual([item["event_type"] for item in first], ["started"])
        self.store.mark_sent(first[0]["id"], now=120)
        second = self.store.claim_due(limit=10, now=125)
        self.assertEqual([item["event_type"] for item in second], ["completed"])

    def test_project_name_is_redacted_before_storage_and_delivery(self):
        self.store.set_enabled(True, now=1)
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.fakeSignatureValue"
        event = HookEvent("secret", "turn", f"/work/incident{jwt}\n", prompt="work")
        self.store.record_start(event, now=10)
        self.store.record_thread_metadata(
            "secret", parent_thread_id=None, source_kind="appServer", now=15
        )
        self.store.finalize_pending(now=15)
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT cwd, project FROM turns").fetchone()
            payload = json.loads(connection.execute("SELECT payload_json FROM outbox").fetchone()[0])
        self.assertEqual(turn["cwd"], "")
        self.assertEqual(turn["project"], "incident[敏感信息已打码]")
        self.assertEqual(payload["project"], "incident[敏感信息已打码]")
        self.assertNotIn(jwt, turn["project"])

    def test_connect_scrubs_legacy_raw_cwd_and_trigger_blocks_new_values(self):
        self._start(finalize=False)
        with closing(sqlite3.connect(self.paths.database)) as connection, connection:
            connection.execute("UPDATE turns SET cwd='/private' WHERE session_id='session-1'")
            connection.execute("DELETE FROM settings WHERE key='raw_cwd_scrubbed_v1'")
        with self.store.managed_connection() as connection:
            self.assertEqual(connection.execute("SELECT cwd FROM turns").fetchone()[0], "")
            connection.execute("UPDATE turns SET cwd='/private-again'")
            self.assertEqual(connection.execute("SELECT cwd FROM turns").fetchone()[0], "")

    def test_concurrent_duplicate_starts_are_atomic(self):
        self.store.set_enabled(True, now=1)
        barrier = threading.Barrier(2)
        errors = []

        def record():
            try:
                barrier.wait(timeout=5)
                NotificationStore(self.paths).record_start(self.event, now=10)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.store.record_thread_metadata(
            "session-1", parent_thread_id=None, source_kind="appServer", now=15
        )
        self.store.finalize_pending(now=15)
        with self.store.managed_connection() as connection:
            counts = (
                connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            )
        self.assertEqual(errors, [])
        self.assertEqual(counts, (1, 1))

    def test_event_keys_do_not_collide_when_ids_contain_colons(self):
        self.store.set_enabled(True, now=1)
        for event in (
            HookEvent("a", "b:c", "", prompt="one"),
            HookEvent("a:b", "c", "", prompt="two"),
        ):
            self.store.record_start(event, now=10)
            self.store.record_thread_metadata(
                event.session_id,
                parent_thread_id=None,
                source_kind="appServer",
                now=15,
            )
        self.store.finalize_pending(now=15)
        with self.store.managed_connection() as connection:
            keys = [row[0] for row in connection.execute("SELECT event_key FROM outbox")]
        self.assertEqual(len(keys), len(set(keys)))

    def test_graceful_off_preserves_pair_but_blocks_new_start(self):
        self.store.set_enabled(True, now=1)
        self._start()
        self.store.set_enabled(False, now=106)
        self.assertFalse(
            self.store.record_completion(
                HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
                now=110,
            )
        )
        self._finish_aggregation(now=115)
        self.store.record_start(HookEvent("session-1", "turn-2", "", prompt="new"), now=111)
        self.store.finalize_pending(now=116)
        with self.store.managed_connection() as connection:
            types = [row[0] for row in connection.execute("SELECT event_type FROM outbox ORDER BY id")]
        self.assertEqual(types, ["started", "completed"])

    def test_immediate_off_suppresses_queued_and_claimed_items(self):
        self.store.set_enabled(True, now=1)
        self._start()
        item = self.store.claim_due(limit=1, now=105)[0]
        self.store.set_enabled(False, immediate=True, now=106)
        self.assertFalse(self.store.is_sendable(item["id"]))
        with self.store.managed_connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM outbox").fetchone()[0], "suppressed")

    def test_enabling_mid_turn_does_not_notify_unverified_completion(self):
        self._start()
        self.store.set_enabled(True, now=106)
        self.assertFalse(
            self.store.record_completion(
                HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
                now=110,
            )
        )
        with self.store.managed_connection() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM outbox").fetchone())

    def test_status_excludes_abandoned_turn_after_retention(self):
        self.store.set_enabled(True, now=1)
        self._start(now=100, finalize=False)
        self.assertEqual(self.store.status_snapshot(now=100)["active_turns"], 1)
        self.assertEqual(
            self.store.status_snapshot(now=100 + OUTBOX_RETENTION_SECONDS)["active_turns"],
            0,
        )

    def test_status_ignores_historical_error_without_active_failed_delivery(self):
        with self.store.managed_connection() as connection:
            connection.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES('last_error', 'old', 1)"
            )

        self.assertIsNone(self.store.status_snapshot(now=100)["last_error"])

    def test_status_reports_error_from_active_retry(self):
        self.store.set_enabled(True, now=1)
        self._start(now=100)
        item = self.store.claim_due(limit=1, now=105)[0]
        self.store.mark_retry(item["id"], "temporary failure", 110)

        self.assertEqual(
            self.store.status_snapshot(now=106)["last_error"],
            "temporary failure",
        )

    def test_status_reports_most_recent_active_failure_not_newest_row(self):
        self.store.set_enabled(True, now=1)
        self._start(now=100)
        first = self.store.claim_due(limit=1, now=105)[0]
        self.store.mark_retry(first["id"], "newest failure", 120, now=110)
        with self.store.managed_connection() as connection:
            connection.execute(
                """INSERT INTO outbox(
                       event_key, event_type, payload_json, status, attempts,
                       next_attempt_at, created_at, last_error, last_error_at
                   ) VALUES('newer-row', 'test', '{}', 'dead', 1, 100, 200,
                            'older failure', 100)"""
            )

        self.assertEqual(
            self.store.status_snapshot(now=111)["last_error"],
            "newest failure",
        )

    def test_status_counts_turn_only_conflicts_without_double_counting_relations(self):
        self.store.record_start(
            HookEvent("shared-agent", "turn-1", "", prompt="shared"), now=10
        )
        self.store.record_start(
            HookEvent("turn-only", "turn-2", "", prompt="turn only"), now=11
        )
        with self.store.managed_connection() as connection:
            connection.execute(
                """INSERT INTO subagents(
                       agent_id, agent_type, parent_session_id, parent_turn_id,
                       started_at, state, relation_state
                   ) VALUES('shared-agent', 'worker', 'parent', 'child-turn',
                            9, 'conflict', 'CONFLICT')"""
            )
            connection.execute(
                """UPDATE turns
                   SET classification='CONFLICT', relation_state='CONFLICT'
                   WHERE session_id IN ('shared-agent', 'turn-only')"""
            )

        self.assertEqual(self.store.status_snapshot()["conflict_relations"], 2)

    def test_claim_lease_recovers_after_worker_crash(self):
        self.store.set_enabled(True, now=1)
        self._start()
        first = self.store.claim_due(limit=1, now=105)
        self.assertEqual(first[0]["attempts"], 1)
        self.assertEqual(self.store.claim_due(limit=1, now=110), [])
        recovered = self.store.claim_due(limit=1, now=200)
        self.assertEqual(recovered[0]["attempts"], 2)

    def test_child_permission_is_silent_even_with_exact_confirmed_root(self):
        self.store.set_enabled(True, now=1)
        self._start(now=10)
        self.store.record_subagent_start(
            SubagentEvent("child-session", "review", "session-1", "child-turn"),
            now=16,
        )
        self.store.record_start(
            HookEvent("child-session", "child-turn", "/work/example", prompt="child"),
            now=17,
        )
        created = self.store.record_permission_request(
            PermissionEvent(
                "child-session",
                "child-turn",
                "mcp__server__tool",
                "a" * 64,
            ),
            now=18,
        )
        self.assertFalse(created)
        with self.store.managed_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type='permission'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_child_request_user_input_requires_exact_confirmed_root(self):
        self.store.set_enabled(True, now=1)
        self.store.set_experimental_capability("request-user-input", True, "fixture")
        self.store.set_experimental_enabled("request-user-input", True)
        self._start(now=10)
        self.store.record_subagent_start(
            SubagentEvent("child-session", "review", "session-1", "child-turn"),
            now=16,
        )
        self.store.record_start(
            HookEvent("child-session", "child-turn", "/work/example", prompt="child"),
            now=17,
        )
        self.assertTrue(
            self.store.record_request_user_input(
                RequestUserInputEvent(
                    "child-session", "child-turn", "request_user_input", "b" * 64
                ),
                now=18,
            )
        )
        with self.store.managed_connection() as connection:
            row = connection.execute(
                "SELECT session_id, turn_id, payload_json FROM outbox "
                "WHERE event_type='experimental_request_user_input'"
            ).fetchone()
        self.assertEqual((row["session_id"], row["turn_id"]), ("session-1", "turn-1"))
        self.assertIn('"child_signal": true', row["payload_json"])

    def test_unverified_root_suppresses_pending_request_user_input(self):
        self.store.set_enabled(True, now=1)
        self.store.set_experimental_capability("request-user-input", True, "fixture")
        self.store.set_experimental_enabled("request-user-input", True)
        self._start(now=10, finalize=False)
        self.assertTrue(
            self.store.record_request_user_input(
                RequestUserInputEvent(
                    "session-1", "turn-1", "request_user_input", "c" * 64
                ),
                now=11,
            )
        )

        self.store.finalize_pending(now=15)

        with self.store.managed_connection() as connection:
            row = connection.execute(
                "SELECT status, last_error FROM outbox "
                "WHERE event_type='experimental_request_user_input'"
            ).fetchone()
        self.assertEqual(row["status"], "suppressed")
        self.assertEqual(row["last_error"], "root identity was not confirmed")

    def test_off_suppresses_request_user_input_waiting_for_root_confirmation(self):
        self.store.set_enabled(True, now=1)
        self.store.set_experimental_capability("request-user-input", True, "fixture")
        self.store.set_experimental_enabled("request-user-input", True)
        self._start(now=10, finalize=False)
        self.assertTrue(
            self.store.record_request_user_input(
                RequestUserInputEvent(
                    "session-1", "turn-1", "request_user_input", "d" * 64
                ),
                now=11,
            )
        )

        self.store.set_enabled(False, now=12)
        self.store.record_thread_metadata(
            "session-1",
            turn_id="turn-1",
            app_thread_id="app-thread",
            parent_thread_id=None,
            source_kind="appServer",
            now=15,
        )
        self.store.finalize_pending(now=15)

        with self.store.managed_connection() as connection:
            row = connection.execute(
                "SELECT status, last_error FROM outbox "
                "WHERE event_type='experimental_request_user_input'"
            ).fetchone()
        self.assertEqual(row["status"], "suppressed")
        self.assertEqual(row["last_error"], "notifications disabled before root confirmation")
        self.assertEqual(self.store.claim_due(limit=10, now=15), [])

    def test_permission_during_root_confirmation_is_never_enqueued(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(self.event, now=100)
        self.assertFalse(
            self.store.record_permission_request(
                PermissionEvent("session-1", "turn-1", "Shell", "e" * 64),
                now=101,
            )
        )
        self.assertEqual(self.store.claim_due(limit=10, now=101), [])

        self.store.record_thread_metadata(
            "session-1",
            turn_id="turn-1",
            app_thread_id="app-thread",
            parent_thread_id=None,
            source_kind="appServer",
            now=105,
        )
        self.store.finalize_pending(now=105)
        started = self.store.claim_due(limit=10, now=105)
        self.assertEqual([item["event_type"] for item in started], ["started"])
        self.store.mark_sent(started[0]["id"], now=105)
        self.assertEqual(self.store.claim_due(limit=10, now=105), [])

    def test_permission_during_unverified_root_confirmation_is_not_persisted(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(self.event, now=100)
        self.assertFalse(
            self.store.record_permission_request(
                PermissionEvent("session-1", "turn-1", "Shell", "f" * 64),
                now=101,
            )
        )

        self.store.finalize_pending(now=105)
        with self.store.managed_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type='permission'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_permission_stays_silent_after_off_now_and_reenable(self):
        self.store.set_enabled(True, now=1)
        self._start(now=10)
        event = PermissionEvent("session-1", "turn-1", "Shell", "b" * 64)
        self.store.set_enabled(False, immediate=True, now=20)
        self.store.set_enabled(True, now=21)
        self.assertFalse(self.store.record_permission_request(event, now=22))
        with self.store.managed_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE event_type='permission'"
                ).fetchone()[0],
                0,
            )

    def test_graceful_off_does_not_restore_permission_notifications(self):
        self.store.set_enabled(True, now=1)
        self._start(now=10)
        self.store.set_enabled(False, now=16)
        self.assertFalse(
            self.store.record_permission_request(
                PermissionEvent("session-1", "turn-1", "Shell", "c" * 64),
                now=17,
            )
        )
        with self.store.managed_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE event_type='permission'"
                ).fetchone()[0],
                0,
            )

    def test_interrupted_observation_and_permission_request_both_stay_silent(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(self.event, now=100)
        self.store.record_thread_metadata(
            "session-1",
            turn_id="turn-1",
            app_thread_id="app-thread",
            parent_thread_id=None,
            source_kind="appServer",
            now=105,
        )
        self.store.finalize_pending(now=105)
        self.store.record_terminal_probe(
            "session-1",
            "turn-1",
            TerminalStatus("turn-1", "interrupted", 100, 106, 6000, None),
            now=106,
        )

        self.assertFalse(
            self.store.record_permission_request(
                PermissionEvent("session-1", "turn-1", "Shell", "d" * 64),
                now=107,
            )
        )
        with self.store.managed_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE event_type='permission'"
                ).fetchone()[0],
                0,
            )
            turn = connection.execute("SELECT * FROM turns").fetchone()
        self.assertEqual(turn["lifecycle"], "RUNNING")
        self.assertIsNone(turn["terminal_status"])

    def test_terminal_race_keeps_first_status_and_one_terminal_event(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(self.event, now=100)
        self.store.record_thread_metadata(
            "session-1",
            turn_id="turn-1",
            app_thread_id="app-thread",
            parent_thread_id=None,
            source_kind="appServer",
            now=105,
        )
        self.store.finalize_pending(now=105)
        self.store.record_completion(
            HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
            now=106,
        )
        self.assertTrue(
            self.store.record_terminal_probe(
                "session-1",
                "turn-1",
                TerminalStatus("turn-1", "failed", 100, 107, 7000, "other"),
                now=107,
            )
        )
        self.assertFalse(
            self.store.record_terminal_probe(
                "session-1",
                "turn-1",
                TerminalStatus("turn-1", "completed", 100, 108, 8000, None),
                now=108,
            )
        )
        self.store.finalize_aggregations(now=112)
        self.store.finalize_aggregations(now=113)
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT * FROM turns").fetchone()
            terminals = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type='completed'"
            ).fetchone()[0]
            conflicts = connection.execute(
                "SELECT value FROM settings WHERE key='terminal_status_conflicts'"
            ).fetchone()[0]
        self.assertEqual(turn["terminal_status"], "failed")
        self.assertEqual(terminals, 1)
        self.assertEqual(conflicts, "1")

    def test_completion_hook_enriches_terminal_probe_before_outbox_finalization(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(self.event, now=100)
        self.store.record_thread_metadata(
            "session-1",
            turn_id="turn-1",
            app_thread_id="app-thread",
            parent_thread_id=None,
            source_kind="appServer",
            now=105,
        )
        self.store.finalize_pending(now=105)
        self.assertTrue(
            self.store.record_terminal_probe(
                "session-1",
                "turn-1",
                TerminalStatus("turn-1", "completed", 100, 106, 6000, None),
                now=106,
            )
        )
        self.assertFalse(
            self.store.record_completion(
                HookEvent(
                    "session-1",
                    "turn-1",
                    "",
                    last_assistant_message="authoritative final summary",
                ),
                now=107,
            )
        )

        self.store.finalize_aggregations(now=111)
        with self.store.managed_connection() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM outbox WHERE event_type='completed'"
                ).fetchone()[0]
            )
        self.assertEqual(payload["summary"], "authoritative final summary")

    def test_completion_fallback_clears_calibration_deadline(self):
        self.store.set_enabled(True, now=1)
        self._start(now=100)
        self.store.record_completion(
            HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
            now=106,
        )

        self.store.finalize_aggregations(now=111)
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT * FROM turns").fetchone()
        self.assertEqual(turn["terminal_status"], "completed")
        self.assertIsNone(turn["terminal_check_due_at"])
        self.assertIsNone(turn["terminal_calibration_deadline"])

    def test_completion_fallback_does_not_require_unavailable_migrated_thread_id(self):
        self.store.set_enabled(True, now=1)
        self._start(now=100)
        with self.store.managed_connection() as connection:
            connection.execute("UPDATE turns SET app_thread_id=NULL")
        self.store.record_completion(
            HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
            now=106,
        )

        self.store.finalize_aggregations(now=111, require_due_probe=True)
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT * FROM turns").fetchone()
        self.assertEqual(turn["terminal_status"], "completed")
        self.assertEqual(turn["terminal_source"], "agent_turn_complete_fallback")

    def test_terminal_observation_starts_fresh_summary_aggregation_window(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(self.event, now=90)
        self.store.record_thread_metadata(
            "session-1",
            turn_id="turn-1",
            app_thread_id="app-thread",
            parent_thread_id=None,
            source_kind="appServer",
            now=95,
        )
        self.store.finalize_pending(now=95)
        self.store.record_terminal_probe(
            "session-1",
            "turn-1",
            TerminalStatus("turn-1", "completed", 90, 100, 10000, None),
            now=110,
        )
        self.store.record_completion(
            HookEvent(
                "session-1",
                "turn-1",
                "",
                last_assistant_message="authoritative summary",
            ),
            now=111,
        )

        self.assertEqual(self.store.finalize_aggregations(now=114), 0)
        self.assertEqual(self.store.finalize_aggregations(now=115), 1)
        with self.store.managed_connection() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM outbox WHERE event_type='completed'"
                ).fetchone()[0]
            )
        self.assertEqual(payload["occurred_at"], 100.0)
        self.assertEqual(payload["summary"], "authoritative summary")

    def test_terminal_scan_expires_after_retention(self):
        self.store.set_enabled(True, now=1)
        self.store.record_start(self.event, now=100)
        self.store.record_thread_metadata(
            "session-1",
            turn_id="turn-1",
            app_thread_id="app-thread",
            parent_thread_id=None,
            source_kind="appServer",
            now=105,
        )
        self.store.finalize_pending(now=105)
        self.assertEqual(
            self.store.pending_terminal_turns(now=100 + OUTBOX_RETENTION_SECONDS),
            [],
        )
        with self.store.managed_connection() as connection:
            stopped = connection.execute(
                "SELECT terminal_scan_stopped_at FROM turns"
            ).fetchone()[0]
        self.assertIsNotNone(stopped)


if __name__ == "__main__":
    unittest.main()
