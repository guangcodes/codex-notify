import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_notify.db import NotificationStore
from codex_notify.hooks import hook_main, process_hook
from codex_notify.paths import AppPaths


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.environment = patch.dict(
            os.environ, {"CODEX_NOTIFY_HOME": self.temp_dir.name}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = NotificationStore(AppPaths(Path(self.temp_dir.name)))
        self.store.set_enabled(True, now=1)
        self.payload = {
            "session_id": "session",
            "turn_id": "turn",
            "cwd": "/projects/demo",
            "prompt": "Do work",
        }

    def test_user_prompt_hook_registers_pending_without_model_visible_output(self):
        output = io.StringIO()
        self.assertEqual(
            hook_main(
                "UserPromptSubmit",
                io.StringIO(json.dumps(self.payload)),
                output,
            ),
            0,
        )
        self.assertEqual(output.getvalue(), "")
        with self.store.managed_connection() as connection:
            turn = connection.execute("SELECT * FROM turns").fetchone()
            outbox_count = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.assertEqual(turn["classification"], "PENDING_ROOT_CANDIDATE")
        self.assertEqual(outbox_count, 0)

    def test_session_start_records_only_lifecycle_source(self):
        process_hook(
            "SessionStart",
            {"session_id": "session", "source": "compact", "cwd": "/private"},
            self.store,
        )
        with self.store.managed_connection() as connection:
            session = connection.execute("SELECT * FROM sessions").fetchone()
            relations = connection.execute("SELECT COUNT(*) FROM subagents").fetchone()[0]
        self.assertEqual(session["source"], "compact")
        self.assertEqual(relations, 0)

    def test_subagent_hooks_store_public_parent_relation_without_outbox(self):
        payload = {
            "agent_id": "agent-1",
            "agent_type": "review",
            "session_id": "parent-session",
            "turn_id": "parent-turn",
            "last_assistant_message": "must not be persisted",
        }
        process_hook("SubagentStart", payload, self.store)
        output = io.StringIO()
        self.assertEqual(
            hook_main("SubagentStop", io.StringIO(json.dumps(payload)), output), 0
        )
        self.assertEqual(json.loads(output.getvalue()), {})
        with self.store.managed_connection() as connection:
            relation = connection.execute("SELECT * FROM subagents").fetchone()
            outbox_count = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.assertEqual(relation["parent_session_id"], "parent-session")
        self.assertEqual(relation["parent_turn_id"], "parent-turn")
        self.assertEqual(relation["state"], "stopped")
        self.assertNotIn("must not be persisted", json.dumps(dict(relation)))
        self.assertEqual(outbox_count, 0)

    def test_subagent_parent_identity_ignores_undocumented_aliases(self):
        process_hook(
            "SubagentStart",
            {
                "session_id": "public-parent-session",
                "turn_id": "public-parent-turn",
                "parent_session_id": "undocumented-session",
                "parent_turn_id": "undocumented-turn",
                "agent_id": "agent-1",
                "agent_type": "worker",
            },
            self.store,
        )
        with self.store.managed_connection() as connection:
            relation = connection.execute(
                "SELECT * FROM subagents WHERE agent_id='agent-1'"
            ).fetchone()
        self.assertEqual(relation["parent_session_id"], "public-parent-session")
        self.assertEqual(relation["parent_turn_id"], "public-parent-turn")

    def test_invalid_identity_is_silent_and_hook_never_blocks_codex(self):
        output = io.StringIO()
        payload = {"session_id": "", "turn_id": "turn", "prompt": "work"}
        self.assertEqual(
            hook_main("UserPromptSubmit", io.StringIO(json.dumps(payload)), output),
            0,
        )
        with self.store.managed_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_invalid_json_is_logged_without_hook_failure(self):
        self.assertEqual(
            hook_main("SessionStart", io.StringIO("not-json"), io.StringIO()), 0
        )
        self.assertTrue(self.store.paths.error_log.exists())

    def test_permission_request_is_always_silent_and_does_not_store_raw_payload(self):
        self.store.record_start(
            self.store_event("session", "turn", "/projects/demo"), now=10
        )
        self.store.record_thread_metadata(
            "session",
            turn_id="turn",
            app_thread_id="thread-app",
            parent_thread_id=None,
            source_kind="appServer",
            now=15,
        )
        self.store.finalize_pending(now=15)
        payload = {
            "hook_event_name": "PermissionRequest",
            "session_id": "session",
            "turn_id": "turn",
            "cwd": "/private/secret/project",
            "model": "model",
            "permission_mode": "default",
            "transcript_path": "/private/transcript.jsonl",
            "tool_name": "Shell",
            "tool_input": {
                "command": "curl https://example.invalid -H 'Authorization: Bearer secret'",
                "reason": "/private/project/full/path.txt",
            },
        }

        process_hook("PermissionRequest", payload, self.store)
        process_hook("PermissionRequest", payload, self.store)

        with self.store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT event_type, payload_json FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["started"])
        database_bytes = self.store.paths.database.read_bytes()
        self.assertNotIn(b"curl", database_bytes)
        self.assertNotIn(b"/private/project/full/path.txt", database_bytes)
        self.assertNotIn(b"Authorization: Bearer secret", database_bytes)
        self.assertNotIn(b"/private/transcript.jsonl", database_bytes)

    def test_permission_request_noop_does_not_construct_store_or_parse_payload(self):
        output = io.StringIO()
        with patch("codex_notify.hooks.NotificationStore") as store_type:
            self.assertEqual(
                hook_main("PermissionRequest", io.StringIO("not-json"), output),
                0,
            )
        store_type.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_permission_request_without_confirmed_turn_is_silent_and_returns_no_decision(self):
        output = io.StringIO()
        result = hook_main(
            "PermissionRequest",
            io.StringIO(
                json.dumps(
                    {
                        "session_id": "unknown",
                        "turn_id": "unknown",
                        "tool_name": "mcp__server__tool",
                        "tool_input": {},
                    }
                )
            ),
            output,
        )
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "")
        with self.store.managed_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_request_user_input_is_exact_best_effort_and_does_not_store_questions(self):
        self.store.set_experimental_capability("request-user-input", True, "schema")
        self.store.set_experimental_enabled("request-user-input", True)
        self.store.record_start(
            self.store_event("session", "turn", "/projects/demo"), now=10
        )
        self.store.record_thread_metadata(
            "session",
            turn_id="turn",
            app_thread_id="thread-app",
            parent_thread_id=None,
            source_kind="appServer",
            now=15,
        )
        self.store.finalize_pending(now=15)
        sensitive = "QUESTION-SECRET-MARKER"
        payload = {
            "session_id": "session",
            "turn_id": "turn",
            "tool_name": "request_user_input",
            "tool_use_id": "item-1",
            "tool_input": {"questions": [{"question": sensitive, "options": []}]},
        }
        process_hook("PreToolUse", payload, self.store)
        process_hook("PreToolUse", payload, self.store)
        process_hook("PreToolUse", {**payload, "tool_name": "other_tool"}, self.store)

        with self.store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT event_type, payload_json FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in rows],
            ["started", "experimental_request_user_input"],
        )
        self.assertNotIn(sensitive, self.store.paths.database.read_bytes().decode("utf-8", "ignore"))
        self.assertIn('"certainty": "best_effort"', rows[-1]["payload_json"])

    @staticmethod
    def store_event(session_id, turn_id, cwd):
        from codex_notify.db import HookEvent

        return HookEvent(session_id, turn_id, cwd, prompt="work")


if __name__ == "__main__":
    unittest.main()
