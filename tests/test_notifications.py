import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_notify.db import HookEvent, NotificationStore
from codex_notify.notifications import process_notification
from codex_notify.paths import AppPaths


class NotificationEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = NotificationStore(AppPaths(Path(self.temp_dir.name)))
        self.store.set_enabled(True)
        self.store.record_start(
            HookEvent("thread-1", "turn-1", "/projects/demo", prompt="Do work"),
            now=100,
        )
        self.store.record_thread_metadata(
            "thread-1", parent_thread_id=None, source_kind="vscode", now=104
        )
        self.store.finalize_pending(now=105)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_agent_turn_complete_queues_definitive_completion(self):
        with patch("codex_notify.db.time.time", return_value=105):
            handled = process_notification(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "thread-1",
                    "turn-id": "turn-1",
                    "cwd": "/projects/demo",
                    "last-assistant-message": "Finished",
                },
                self.store,
            )
        self.assertFalse(handled)
        self.store.finalize_aggregations(now=110)
        with self.store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT event_type, payload_json FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["started", "completed"])
        self.assertIn('"event_id":', rows[1]["payload_json"])

    def test_ignores_unknown_notification_type(self):
        self.assertFalse(process_notification({"type": "approval-requested"}, self.store))


if __name__ == "__main__":
    unittest.main()
