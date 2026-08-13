import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from codex_notify.constants import OUTBOX_RETENTION_SECONDS
from codex_notify.db import HookEvent, NotificationStore
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
            self.store.finalize_pending(now=now + 5)

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

    def test_enabled_completion_without_start_queues_standalone(self):
        self.store.set_enabled(True, now=1)
        self.assertTrue(
            self.store.record_completion(
                HookEvent("missing", "turn", "", last_assistant_message="done"),
                now=10,
            )
        )
        claimed = self.store.claim_due(limit=10, now=10)
        self.assertEqual([item["event_type"] for item in claimed], ["completed"])
        self.assertTrue(claimed[0]["payload"]["incomplete_lifecycle"])

    def test_enabled_turn_queues_exactly_one_ordered_pair(self):
        self.store.set_enabled(True, now=1)
        self._start()
        self._start(now=101)
        completion = HookEvent("session-1", "turn-1", "", last_assistant_message="done")
        self.assertTrue(self.store.record_completion(completion, now=120))
        self.assertTrue(self.store.record_completion(completion, now=121))
        with self.store.managed_connection() as connection:
            rows = connection.execute("SELECT event_type FROM outbox ORDER BY id").fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["started", "completed"])
        first = self.store.claim_due(limit=10, now=120)
        self.assertEqual([item["event_type"] for item in first], ["started"])
        self.store.mark_sent(first[0]["id"], now=120)
        second = self.store.claim_due(limit=10, now=121)
        self.assertEqual([item["event_type"] for item in second], ["completed"])

    def test_project_name_is_redacted_before_storage_and_delivery(self):
        self.store.set_enabled(True, now=1)
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.fakeSignatureValue"
        event = HookEvent("secret", "turn", f"/work/incident{jwt}\n", prompt="work")
        self.store.record_start(event, now=10)
        self.store.finalize_pending(now=15)
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT cwd, project FROM turns").fetchone()
            payload = json.loads(connection.execute("SELECT payload_json FROM outbox").fetchone()[0])
        warning = "内容可能包含敏感信息，请回到 Codex 查看。"
        self.assertEqual(turn["cwd"], "")
        self.assertEqual(turn["project"], warning)
        self.assertEqual(payload["project"], warning)

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
        self.store.finalize_pending(now=15)
        with self.store.managed_connection() as connection:
            keys = [row[0] for row in connection.execute("SELECT event_key FROM outbox")]
        self.assertEqual(len(keys), len(set(keys)))

    def test_graceful_off_preserves_pair_but_blocks_new_start(self):
        self.store.set_enabled(True, now=1)
        self._start()
        self.store.set_enabled(False, now=106)
        self.assertTrue(
            self.store.record_completion(
                HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
                now=110,
            )
        )
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

    def test_enabling_mid_turn_queues_standalone_completion(self):
        self._start()
        self.store.set_enabled(True, now=106)
        self.assertTrue(
            self.store.record_completion(
                HookEvent("session-1", "turn-1", "", last_assistant_message="done"),
                now=110,
            )
        )
        with self.store.managed_connection() as connection:
            row = connection.execute("SELECT event_type, payload_json FROM outbox").fetchone()
        self.assertEqual(row["event_type"], "completed")
        self.assertTrue(json.loads(row["payload_json"])["incomplete_lifecycle"])

    def test_status_excludes_abandoned_turn_after_retention(self):
        self.store.set_enabled(True, now=1)
        self._start(now=100, finalize=False)
        self.assertEqual(self.store.status_snapshot(now=100)["active_turns"], 1)
        self.assertEqual(
            self.store.status_snapshot(now=100 + OUTBOX_RETENTION_SECONDS)["active_turns"],
            0,
        )

    def test_claim_lease_recovers_after_worker_crash(self):
        self.store.set_enabled(True, now=1)
        self._start()
        first = self.store.claim_due(limit=1, now=105)
        self.assertEqual(first[0]["attempts"], 1)
        self.assertEqual(self.store.claim_due(limit=1, now=110), [])
        recovered = self.store.claim_due(limit=1, now=200)
        self.assertEqual(recovered[0]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
