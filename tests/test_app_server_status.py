import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from codex_notify.app_server_status import (
    AppServerStatusReader,
    TerminalStatus,
    read_terminal_status,
)
from codex_notify.paths import AppPaths


class _NonClosingStringIO(io.StringIO):
    def close(self):
        pass


class _Process:
    def __init__(self, lines):
        self.stdin = _NonClosingStringIO()
        self.stdout = _NonClosingStringIO(
            "".join(json.dumps(line) + "\n" for line in lines)
        )
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _turn(status="completed", **updates):
    value = {
        "id": "turn-1",
        "status": status,
        "startedAt": 10,
        "completedAt": 15,
        "durationMs": 5000,
        "error": None,
        "items": [],
        "itemsView": "notLoaded",
    }
    value.update(updates)
    return value


class AppServerStatusTests(unittest.TestCase):
    def _read(self, turn):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {"data": [turn], "nextCursor": None},
                },
            ]
        )
        result = AppServerStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        ).read("thread-1", "turn-1")
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(
            requests[-1],
            {
                "id": 2,
                "method": "thread/turns/list",
                "params": {
                    "threadId": "thread-1",
                    "limit": 20,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            },
        )
        self.assertNotIn("thread/read", process.stdin.getvalue())
        self.assertTrue(process.terminated)
        return result

    def test_parses_completed(self):
        self.assertEqual(
            self._read(_turn()),
            TerminalStatus("turn-1", "completed", 10, 15, 5000, None),
        )

    def test_parses_interrupted(self):
        self.assertEqual(self._read(_turn("interrupted")).status, "interrupted")

    def test_parses_failed_with_structured_category_only(self):
        status = self._read(
            _turn(
                "failed",
                error={
                    "message": "private provider text",
                    "additionalDetails": "secret details",
                    "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}},
                },
            )
        )
        self.assertEqual(status.error_category, "httpConnectionFailed")
        self.assertNotIn("private provider text", repr(status))
        self.assertNotIn("secret details", repr(status))

    def test_in_progress_is_a_successful_nonterminal_read(self):
        status = self._read(
            _turn("inProgress", completedAt=None, durationMs=None, error=None)
        )
        self.assertEqual(status.status, "inProgress")

    def test_rejects_non_not_loaded_items_view(self):
        self.assertIsNone(self._read(_turn(itemsView="summary")))

    def test_rejects_nonempty_items(self):
        self.assertIsNone(self._read(_turn(items=[{"type": "message"}])))

    def test_rejects_unknown_turn_field_and_status(self):
        self.assertIsNone(self._read(_turn(secretPreview="private")))
        self.assertIsNone(self._read(_turn("cancelled")))

    def test_rejects_unknown_error_category(self):
        self.assertIsNone(
            self._read(
                _turn(
                    "failed",
                    error={"message": "x", "codexErrorInfo": {"newKind": {}}},
                )
            )
        )

    def test_rejects_malformed_error_structure(self):
        malformed = (
            {"codexErrorInfo": "other"},
            {"message": 123, "codexErrorInfo": "other"},
            {"message": "x", "additionalDetails": {}, "codexErrorInfo": "other"},
            {
                "message": "x",
                "codexErrorInfo": {
                    "httpConnectionFailed": {"httpStatusCode": True}
                },
            },
            {
                "message": "x",
                "codexErrorInfo": {
                    "httpConnectionFailed": {"httpStatusCode": 70000}
                },
            },
            {
                "message": "x",
                "codexErrorInfo": {"httpConnectionFailed": {"privateText": "x"}},
            },
            {
                "message": "x",
                "codexErrorInfo": {"activeTurnNotSteerable": {"turnKind": "other"}},
            },
        )
        for error in malformed:
            with self.subTest(error=error):
                self.assertIsNone(self._read(_turn("failed", error=error)))

    def test_accepts_known_non_steerable_error_category(self):
        status = self._read(
            _turn(
                "failed",
                error={
                    "message": "private",
                    "codexErrorInfo": {
                        "activeTurnNotSteerable": {"turnKind": "review"}
                    },
                },
            )
        )
        self.assertEqual(status.error_category, "activeTurnNotSteerable")

    def test_error_response_and_invalid_identity_fail_silent(self):
        process = _Process([{"id": 1, "result": {}}, {"id": 2, "error": {}}])
        reader = AppServerStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        )
        self.assertIsNone(reader.read("thread-1", "turn-1"))
        self.assertIsNone(reader.read("", "turn-1"))

    def test_process_failure_fails_silent(self):
        reader = AppServerStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
        )
        self.assertIsNone(reader.read("thread-1", "turn-1"))

    def test_wrapper_uses_shared_nonblocking_probe_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths(Path(directory))
            paths.ensure_runtime_dirs()
            lock_path = paths.data_dir / "app-server-probe.lock"
            lock_path.write_text("", encoding="utf-8")
            import fcntl

            with lock_path.open("r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(
                    read_terminal_status(paths, "thread-1", "turn-1", binary=Path("/codex"))
                )

    def test_oversized_output_fails_silent(self):
        huge = "x" * (1024 * 1024 + 1)
        process = _Process([{"id": 1, "result": {}, "padding": huge}])
        result = AppServerStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        ).read("thread-1", "turn-1")
        self.assertIsNone(result)
        self.assertTrue(process.terminated)

    def test_timeout_fails_silent_and_terminates(self):
        class SlowStream:
            def readline(self, _limit=-1):
                time.sleep(0.05)
                return ""

            def close(self):
                pass

        process = _Process([])
        process.stdout = SlowStream()
        result = AppServerStatusReader(
            Path("/codex"),
            timeout_seconds=0.01,
            popen=lambda *args, **kwargs: process,
        ).read("thread-1", "turn-1")
        self.assertIsNone(result)
        self.assertTrue(process.terminated)

    def test_server_request_is_ignored_without_response(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {"id": "server-request", "method": "tool/requestUserInput", "params": {}},
                {"id": 2, "result": {"data": [_turn()], "nextCursor": None}},
            ]
        )
        result = AppServerStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        ).read("thread-1", "turn-1")
        self.assertEqual(result.status, "completed")
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(
            [request["method"] for request in requests],
            ["initialize", "initialized", "thread/turns/list"],
        )

    def test_missing_optional_timestamps_are_accepted(self):
        turn = {
            "id": "turn-1",
            "status": "inProgress",
            "items": [],
            "itemsView": "notLoaded",
        }
        status = self._read(turn)
        self.assertEqual(
            status,
            TerminalStatus("turn-1", "inProgress", None, None, None, None),
        )


if __name__ == "__main__":
    unittest.main()
