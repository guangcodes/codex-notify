import io
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_notify.app_server_metadata import (
    MAX_STDOUT_BYTES,
    AppServerMetadataReader,
    ThreadMetadata,
    find_bundled_codex,
    read_pending_metadata,
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


class AppServerMetadataTests(unittest.TestCase):
    def test_reads_only_thread_metadata_and_terminates(self):
        process = _Process(
            [
                {"id": 1, "result": {"userAgent": "Codex Desktop/1"}},
                {"method": "remoteControl/status/changed", "params": {}},
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thread-1",
                            "parentThreadId": None,
                            "source": "vscode",
                            "createdAt": 10,
                            "turns": [],
                            "preview": "must not be persisted",
                        }
                    },
                },
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "turn-1",
                                "items": [],
                                "itemsView": "notLoaded",
                                "status": "completed",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )
        reader = AppServerMetadataReader(Path("/Applications/ChatGPT.app/codex"), popen=lambda *a, **k: process)

        self.assertEqual(
            reader.read(
                ["thread-1"], expected_turn_ids={"thread-1": "turn-1"}
            ),
            {
                "thread-1": ThreadMetadata(
                    thread_id="thread-1",
                    parent_thread_id=None,
                    source_kind="vscode",
                    created_at=10,
                )
            },
        )
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(
            [item["method"] for item in requests],
            ["initialize", "initialized", "thread/read", "thread/turns/list"],
        )
        self.assertEqual(
            requests[2]["params"], {"threadId": "thread-1", "includeTurns": False}
        )
        self.assertNotIn("thread/list", process.stdin.getvalue())
        self.assertTrue(process.terminated)

    def test_subagent_source_is_normalized(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "child",
                            "parentThreadId": "parent",
                            "source": {"subAgent": "review"},
                            "createdAt": 11,
                        }
                    },
                },
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "child-turn",
                                "items": [],
                                "itemsView": "notLoaded",
                                "status": "completed",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )
        metadata = AppServerMetadataReader(
            Path("/codex"), popen=lambda *a, **k: process
        ).read(["child"], expected_turn_ids={"child": "child-turn"})["child"]
        self.assertEqual(metadata.source_kind, "subAgent")
        self.assertEqual(metadata.parent_thread_id, "parent")

    def test_resolves_hook_session_id_through_metadata_only_thread_list(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "error": {"code": -32600, "message": "thread not loaded"}},
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "app-thread",
                                "sessionId": "hook-session",
                                "parentThreadId": None,
                                "source": "appServer",
                                "createdAt": 20,
                                "preview": "secret preview must be ignored",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
                {
                    "id": 4,
                    "result": {
                        "thread": {
                            "id": "app-thread",
                            "sessionId": "hook-session",
                            "parentThreadId": None,
                            "source": "appServer",
                            "createdAt": 20,
                            "preview": "secret preview must be ignored",
                        }
                    },
                },
                {
                    "id": 5,
                    "result": {
                        "data": [
                            {
                                "id": "hook-turn",
                                "items": [],
                                "itemsView": "notLoaded",
                                "status": "completed",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )
        reader = AppServerMetadataReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        )

        metadata = reader.read(
            ["hook-session"],
            expected_turn_ids={"hook-session": "hook-turn"},
        )
        self.assertEqual(
            metadata,
            {
                "hook-session": ThreadMetadata(
                    thread_id="app-thread",
                    parent_thread_id=None,
                    source_kind="appServer",
                    created_at=20,
                )
            },
        )
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(
            [item["method"] for item in requests],
            [
                "initialize",
                "initialized",
                "thread/read",
                "thread/list",
                "thread/read",
                "thread/turns/list",
            ],
        )
        self.assertEqual(
            requests[-1]["params"],
            {
                "threadId": "app-thread",
                "limit": 20,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            },
        )
        self.assertEqual(
            requests[3]["params"],
            {
                "limit": 20,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "sourceKinds": ["vscode", "appServer", "cli"],
                "useStateDbOnly": True,
            },
        )
        self.assertNotIn("secret preview", repr(metadata))

    def test_ambiguous_session_mapping_remains_unknown(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "error": {"code": -32600}},
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "thread-a",
                                "sessionId": "hook-session",
                                "source": "vscode",
                            },
                            {
                                "id": "thread-b",
                                "sessionId": "hook-session",
                                "source": "appServer",
                            },
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )

        self.assertEqual(
            AppServerMetadataReader(
                Path("/codex"), popen=lambda *args, **kwargs: process
            ).read(
                ["hook-session"],
                expected_turn_ids={"hook-session": "hook-turn"},
            ),
            {},
        )
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual([item["method"] for item in requests].count("thread/read"), 1)

    def test_mapping_is_not_unique_when_later_page_has_same_session(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "error": {"code": -32600}},
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "thread-a",
                                "sessionId": "hook-session",
                                "source": "vscode",
                            }
                        ],
                        "nextCursor": "page-2",
                    },
                },
                {
                    "id": 4,
                    "result": {
                        "thread": {
                            "id": "thread-a",
                            "parentThreadId": None,
                            "source": "vscode",
                            "createdAt": 20,
                        },
                        "data": [
                            {
                                "id": "thread-b",
                                "sessionId": "hook-session",
                                "source": "appServer",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )

        self.assertEqual(
            AppServerMetadataReader(
                Path("/codex"), popen=lambda *args, **kwargs: process
            ).read(
                ["hook-session"],
                expected_turn_ids={"hook-session": "hook-turn"},
            ),
            {},
        )
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(requests[-1]["method"], "thread/list")
        self.assertEqual(requests[-1]["params"]["cursor"], "page-2")

    def test_final_read_must_confirm_the_mapped_session_id(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "error": {"code": -32600}},
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "app-thread",
                                "sessionId": "hook-session",
                                "source": "appServer",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
                {
                    "id": 4,
                    "result": {
                        "thread": {
                            "id": "app-thread",
                            "sessionId": "different-session",
                            "parentThreadId": None,
                            "source": "appServer",
                            "createdAt": 20,
                        }
                    },
                },
            ]
        )

        self.assertEqual(
            AppServerMetadataReader(
                Path("/codex"), popen=lambda *args, **kwargs: process
            ).read(
                ["hook-session"],
                expected_turn_ids={"hook-session": "hook-turn"},
            ),
            {},
        )

    def test_session_tree_root_is_not_accepted_for_a_different_turn(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "error": {"code": -32600}},
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "root-thread",
                                "sessionId": "shared-session",
                                "source": "appServer",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
                {
                    "id": 4,
                    "result": {
                        "thread": {
                            "id": "root-thread",
                            "sessionId": "shared-session",
                            "parentThreadId": None,
                            "source": "appServer",
                            "createdAt": 20,
                        }
                    },
                },
                {
                    "id": 5,
                    "result": {
                        "data": [
                            {
                                "id": "root-turn",
                                "items": [],
                                "itemsView": "notLoaded",
                                "status": "completed",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )

        self.assertEqual(
            AppServerMetadataReader(
                Path("/codex"), popen=lambda *args, **kwargs: process
            ).read(
                ["shared-session"],
                expected_turn_ids={"shared-session": "child-turn"},
            ),
            {},
        )

    def test_direct_thread_read_must_also_confirm_turn_ownership(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "root-thread",
                            "parentThreadId": None,
                            "source": "appServer",
                            "createdAt": 20,
                        }
                    },
                },
                {
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "id": "different-turn",
                                "items": [],
                                "itemsView": "notLoaded",
                                "status": "completed",
                            }
                        ],
                        "nextCursor": None,
                    },
                },
            ]
        )

        self.assertEqual(
            AppServerMetadataReader(
                Path("/codex"), popen=lambda *args, **kwargs: process
            ).read(
                ["root-thread"],
                expected_turn_ids={"root-thread": "expected-turn"},
            ),
            {},
        )

    def test_protocol_error_is_fail_silent(self):
        process = _Process([{"id": 1, "error": {"code": -32600}}])
        self.assertEqual(
            AppServerMetadataReader(Path("/codex"), popen=lambda *a, **k: process).read(
                ["thread"], expected_turn_ids={"thread": "turn"}
            ),
            {},
        )
        self.assertTrue(process.terminated)

    def test_process_start_failure_is_fail_silent(self):
        def fail(*_args, **_kwargs):
            raise OSError("unavailable")

        self.assertEqual(
            AppServerMetadataReader(Path("/codex"), popen=fail).read(
                ["thread"], expected_turn_ids={"thread": "turn"}
            ),
            {},
        )

    def test_oversized_line_is_fail_silent(self):
        process = _Process([])
        process.stdout = _NonClosingStringIO("x" * (MAX_STDOUT_BYTES + 1) + "\n")
        self.assertEqual(
            AppServerMetadataReader(Path("/codex"), popen=lambda *a, **k: process).read(
                ["thread"], expected_turn_ids={"thread": "turn"}
            ),
            {},
        )

    def test_reader_stops_consuming_stdout_at_byte_limit(self):
        padding = "x" * 300
        process = _Process(
            [{"id": 1, "result": {}}]
            + [
                {"method": "event", "params": {"padding": padding}}
                for _ in range(10_000)
            ]
        )

        self.assertEqual(
            AppServerMetadataReader(
                Path("/codex"), popen=lambda *a, **k: process
            ).read(["thread"], expected_turn_ids={"thread": "turn"}),
            {},
        )
        self.assertLessEqual(process.stdout.tell(), MAX_STDOUT_BYTES + 1024)

    def test_missing_metadata_field_is_unknown(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "thread": {
                            "id": "thread",
                            "source": "vscode",
                            "createdAt": 1,
                        }
                    },
                },
            ]
        )
        self.assertEqual(
            AppServerMetadataReader(
                Path("/codex"), popen=lambda *a, **k: process
            ).read(["thread"], expected_turn_ids={"thread": "turn"}),
            {},
        )

    def test_finds_only_bundled_desktop_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("binary")
            binary.chmod(0o700)
            self.assertEqual(find_bundled_codex(application_roots=(root,)), binary)
        with patch("shutil.which", return_value="/usr/local/bin/codex"):
            self.assertIsNone(find_bundled_codex(application_roots=()))

    def test_busy_probe_lock_returns_without_starting_app_server(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths(Path(directory))
            paths.ensure_runtime_dirs()
            lock_path = paths.data_dir / "metadata-probe.lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    patch(
                        "codex_notify.app_server_metadata.find_bundled_codex",
                        return_value=Path("/codex"),
                    ),
                    patch.object(AppServerMetadataReader, "read") as reader,
                ):
                    self.assertIsNone(
                        read_pending_metadata(paths, ["thread"], {"thread": "turn"})
                    )
                    reader.assert_not_called()
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
