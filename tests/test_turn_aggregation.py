import json
import tempfile
import unittest
from pathlib import Path

from codex_notify.db import HookEvent, NotificationStore, SubagentEvent
from codex_notify.paths import AppPaths


class TurnAggregationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = NotificationStore(AppPaths(Path(self.temp_dir.name)))
        self.store.set_enabled(True, now=1)
        self.store.record_start(
            HookEvent("root-session", "root-turn", "/work/demo", prompt="root"),
            now=10,
        )
        self.store.record_thread_metadata(
            "root-session", parent_thread_id=None, source_kind="appServer", now=14
        )
        self.store.finalize_pending(now=15)

    def _start_child(self, index, *, started_at):
        session_id = f"child-{index}"
        turn_id = f"child-turn-{index}"
        self.assertTrue(
            self.store.record_subagent_start(
                SubagentEvent(
                    session_id,
                    f"worker-{index}",
                    "root-session",
                    turn_id,
                ),
                now=started_at,
            )
        )
        self.store.record_start(
            HookEvent(session_id, turn_id, "/work/demo", prompt="child"),
            now=started_at + 0.1,
        )

    def _complete_child(self, index, *, completed_at, result):
        self.assertFalse(
            self.store.record_completion(
                HookEvent(
                    f"child-{index}",
                    f"child-turn-{index}",
                    "/work/demo",
                    last_assistant_message=result,
                ),
                now=completed_at,
            )
        )

    def _child(self, index, *, started_at, result):
        self._start_child(index, started_at=started_at)
        self._complete_child(index, completed_at=started_at + 0.2, result=result)

    def _completion_payload(self):
        with self.store.managed_connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM outbox WHERE event_type='completed'"
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def test_confirmed_children_merge_in_subagent_start_order(self):
        self._start_child(2, started_at=12)
        self._child(1, started_at=11, result="first result")
        self.store.record_completion(
            HookEvent(
                "root-session",
                "root-turn",
                "/work/demo",
                last_assistant_message="parent result",
            ),
            now=14,
        )
        self._complete_child(2, completed_at=18, result="second result")

        self.assertEqual(self.store.finalize_aggregations(now=18.999), 0)
        self.assertEqual(self.store.finalize_aggregations(now=19), 1)
        payload = self._completion_payload()
        self.assertEqual(payload["summary"], "parent result")
        self.assertEqual(
            payload["child_results"],
            [
                {"agent_type": "worker-1", "summary": "first result"},
                {"agent_type": "worker-2", "summary": "second result"},
            ],
        )

    def test_child_arriving_after_snapshot_does_not_change_notification(self):
        self._start_child(1, started_at=11)
        self.store.record_completion(
            HookEvent(
                "root-session",
                "root-turn",
                "/work/demo",
                last_assistant_message="parent result",
            ),
            now=14,
        )
        self.assertEqual(self.store.finalize_aggregations(now=19), 1)
        self._complete_child(1, completed_at=20, result="late result")

        payload = self._completion_payload()
        self.assertEqual(payload["child_results"], [])
        with self.store.managed_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type='completed'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_delayed_worker_excludes_child_completed_after_fixed_window(self):
        self._start_child(1, started_at=11)
        self.store.record_completion(
            HookEvent(
                "root-session",
                "root-turn",
                "/work/demo",
                last_assistant_message="parent result",
            ),
            now=14,
        )
        self._complete_child(1, completed_at=20, result="late result")

        self.assertEqual(self.store.finalize_aggregations(now=25), 1)
        self.assertEqual(self._completion_payload()["child_results"], [])

    def test_conflicting_agent_identity_is_not_merged(self):
        self._child(1, started_at=11, result="must stay hidden")
        self.assertFalse(
            self.store.record_subagent_start(
                SubagentEvent(
                    "child-1",
                    "other-worker",
                    "root-session",
                    "other-turn",
                ),
                now=12,
            )
        )
        self.store.record_completion(
            HookEvent(
                "root-session",
                "root-turn",
                "/work/demo",
                last_assistant_message="parent result",
            ),
            now=14,
        )
        self.store.finalize_aggregations(now=19)

        self.assertEqual(self._completion_payload()["child_results"], [])
        with self.store.managed_connection() as connection:
            child = connection.execute(
                "SELECT classification FROM turns WHERE session_id='child-1'"
            ).fetchone()[0]
        self.assertEqual(child, "CONFLICT")

    def test_only_eight_children_are_shown_and_results_are_sanitized(self):
        for index in range(9):
            result = (
                "Bearer abcdefghijklmnopqrstuvwxyz"
                if index == 0
                else "x" * 250
            )
            self._child(index, started_at=11 + index * 0.1, result=result)
        self.store.record_completion(
            HookEvent(
                "root-session",
                "root-turn",
                "/work/demo",
                last_assistant_message="parent result",
            ),
            now=14,
        )
        self.store.finalize_aggregations(now=19)

        payload = self._completion_payload()
        self.assertEqual(len(payload["child_results"]), 8)
        self.assertEqual(payload["omitted_child_results"], 1)
        self.assertEqual(
            payload["child_results"][0]["summary"],
            "内容可能包含敏感信息，请回到 Codex 查看。",
        )
        self.assertLessEqual(len(payload["child_results"][1]["summary"]), 200)

    def test_off_now_permanently_cancels_pending_root_aggregation(self):
        self.store.record_completion(
            HookEvent(
                "root-session",
                "root-turn",
                "/work/demo",
                last_assistant_message="parent result",
            ),
            now=14,
        )
        self.store.set_enabled(False, immediate=True, now=15)
        self.store.set_enabled(True, now=16)

        self.assertEqual(self.store.finalize_aggregations(now=100), 0)
        with self.store.managed_connection() as connection:
            turn = connection.execute(
                "SELECT suppressed, aggregation_due_at FROM turns "
                "WHERE session_id='root-session'"
            ).fetchone()
            completions = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type='completed'"
            ).fetchone()[0]
        self.assertEqual(turn["suppressed"], 1)
        self.assertIsNone(turn["aggregation_due_at"])
        self.assertEqual(completions, 0)


if __name__ == "__main__":
    unittest.main()
