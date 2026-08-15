import json
import tempfile
import unittest
from pathlib import Path

from codex_notify.db import HookEvent, NotificationStore, SubagentEvent
from codex_notify.paths import AppPaths


class PublicTurnPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = NotificationStore(AppPaths(Path(self.temp_dir.name)))
        self.store.set_enabled(True, now=1)

    def _events(self):
        with self.store.managed_connection() as connection:
            return connection.execute(
                "SELECT event_type, session_id, turn_id, payload_json "
                "FROM outbox ORDER BY id"
            ).fetchall()

    def test_metadata_confirmed_root_is_notified_after_window(self):
        event = HookEvent("session", "turn", "/work/demo", prompt="ordinary prompt")

        self.assertFalse(self.store.record_start(event, now=10))
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT * FROM turns").fetchone()
        self.assertEqual(turn["classification"], "PENDING_ROOT_CANDIDATE")
        self.assertEqual(turn["lifecycle"], "RUNNING")
        self.assertEqual(turn["decision_due_at"], 15)
        self.assertEqual(self._events(), [])

        self.assertEqual(self.store.finalize_pending(now=14.999), 0)
        self.assertTrue(
            self.store.record_thread_metadata(
                "session", parent_thread_id=None, source_kind="vscode", now=14
            )
        )
        self.assertEqual(self.store.finalize_pending(now=15), 1)
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT * FROM turns").fetchone()
        self.assertEqual(turn["classification"], "NOTIFIABLE_ROOT")
        self.assertEqual([row["event_type"] for row in self._events()], ["started"])

    def test_unknown_prompt_is_silent_after_window(self):
        prompt = (
            "# Overview\nGenerate 0 to 3 hyperpersonalized suggestions for what this "
            "user can do with Codex in this local project: demo.\n\n# Rules\n"
            "- Be concise\n# Response format\nReturn JSON with 0 to 3 suggestions."
        )
        self.store.record_start(HookEvent("session", "turn", "", prompt=prompt), now=10)
        self.assertEqual(self.store.finalize_pending(now=15), 1)
        with self.store.managed_connection() as connection:
            classification = connection.execute("SELECT classification FROM turns").fetchone()[0]
        self.assertEqual(classification, "UNVERIFIED")
        self.assertEqual(self._events(), [])

    def test_exact_child_identity_and_unique_parent_are_merged_not_notified(self):
        self.store.record_start(
            HookEvent("parent-session", "root-turn", "/work/demo", prompt="root"),
            now=8,
        )
        self.store.record_subagent_start(
            SubagentEvent("child-session", "review", "parent-session", "child-turn"),
            now=9,
        )
        self.store.record_start(
            HookEvent("child-session", "child-turn", "/work/demo", prompt="internal"),
            now=10,
        )

        self.assertEqual(self.store.finalize_pending(now=15), 1)
        with self.store.managed_connection() as connection:
            turn = connection.execute(
                "SELECT classification FROM turns WHERE session_id='child-session'"
            ).fetchone()
        self.assertEqual(turn["classification"], "CONFIRMED_CHILD")
        self.assertEqual(self._events(), [])

    def test_external_relation_cannot_consume_exact_parent_completion(self):
        parent = HookEvent("parent-session", "shared-turn", "/work/demo", prompt="root")
        self.store.record_start(parent, now=10)
        with self.store.managed_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE managed_launchers(
                    launcher_id TEXT PRIMARY KEY, kind TEXT, canonical_path TEXT,
                    sha256 TEXT, owner_uid INTEGER, contract_version INTEGER,
                    enabled INTEGER
                );
                CREATE TABLE launch_intents(
                    id TEXT PRIMARY KEY, launcher_id TEXT, kind TEXT,
                    parent_session_id TEXT, parent_turn_id TEXT, tool_use_id TEXT,
                    cwd_fingerprint TEXT, argv_fingerprint TEXT, issued_at REAL,
                    claim_deadline REAL, state TEXT, proof_hash TEXT,
                    proof_deadline REAL, claimed_at REAL, consumed_at REAL
                );
                CREATE TABLE external_task_relations(
                    id TEXT PRIMARY KEY, kind TEXT, source TEXT,
                    parent_session_id TEXT, parent_turn_id TEXT,
                    child_session_id TEXT, child_turn_id TEXT, intent_id TEXT,
                    started_at REAL, completed_at REAL, state TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO managed_launchers VALUES"
                "('legacy', 'reviewer', '/legacy/reviewer', 'digest', 1, 1, 0)"
            )
            connection.execute(
                """INSERT INTO launch_intents(
                       id, launcher_id, kind, parent_session_id, parent_turn_id,
                       tool_use_id, cwd_fingerprint, argv_fingerprint, issued_at,
                       claim_deadline, state, proof_hash, proof_deadline, claimed_at,
                       consumed_at
                   ) VALUES(
                       'intent', 'legacy', 'reviewer', 'parent-session', 'shared-turn',
                       'tool', 'cwd', 'argv', 1, 2, 'CONSUMED', 'proof', 3, 2, 3
                   )"""
            )
            connection.execute(
                """INSERT INTO external_task_relations(
                       id, kind, source, parent_session_id, parent_turn_id,
                       child_session_id, child_turn_id, intent_id, started_at,
                       state
                   ) VALUES(
                       'relation', 'reviewer', 'legacy', 'parent-session', 'shared-turn',
                       'child-session', 'shared-turn', 'intent', 3, 'ACTIVE'
                   )"""
            )

        self.assertFalse(
            self.store.record_completion(
                HookEvent(
                    "parent-session",
                    "shared-turn",
                    "/work/demo",
                    last_assistant_message="parent done",
                ),
                now=12,
            )
        )
        with self.store.managed_connection() as connection:
            parent_row = connection.execute(
                "SELECT lifecycle, pending_completed_at FROM turns "
                "WHERE session_id='parent-session' AND turn_id='shared-turn'"
            ).fetchone()
            relation = connection.execute(
                "SELECT state FROM external_task_relations WHERE id='relation'"
            ).fetchone()
        self.assertEqual(parent_row["lifecycle"], "COMPLETED")
        self.assertEqual(parent_row["pending_completed_at"], 12)
        self.assertEqual(relation["state"], "ACTIVE")

    def test_same_turn_id_in_another_session_never_completes_parent(self):
        self.store.record_start(
            HookEvent("parent-session", "shared-turn", "", prompt="root"), now=10
        )
        self.assertFalse(
            self.store.record_completion(
                HookEvent(
                    "child-session",
                    "shared-turn",
                    "",
                    last_assistant_message="child done",
                ),
                now=12,
            )
        )
        with self.store.managed_connection() as connection:
            parent = connection.execute(
                "SELECT lifecycle FROM turns WHERE session_id='parent-session'"
            ).fetchone()
            child = connection.execute(
                "SELECT lifecycle FROM turns WHERE session_id='child-session'"
            ).fetchone()
        self.assertEqual(parent["lifecycle"], "RUNNING")
        self.assertEqual(child["lifecycle"], "COMPLETED")

    def test_fresh_database_has_no_private_reviewer_schema(self):
        with self.store.managed_connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(
            {"managed_launchers", "launch_intents", "external_task_relations"}.isdisjoint(
                tables
            )
        )

    def test_event_key_is_the_exact_identity_tuple_encoding(self):
        self.store.record_start(HookEvent("s:1", "t:1", "", prompt="work"), now=10)
        self.store.record_thread_metadata(
            "s:1", parent_thread_id=None, source_kind="vscode", now=14
        )
        self.store.finalize_pending(now=15)
        row = self._events()[0]
        with self.store.managed_connection() as connection:
            event_key = connection.execute("SELECT event_key FROM outbox").fetchone()[0]
        self.assertEqual(event_key, json.dumps(["s:1", "t:1", "started"], separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()
