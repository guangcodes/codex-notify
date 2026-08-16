import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_notify.experimental_status import (
    ExperimentalSnapshot,
    ExperimentalStatusReader,
    McpAuthObservation,
    RateLimitObservation,
    probe_experimental_capabilities,
)
from codex_notify.db import NotificationStore
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


class ExperimentalStatusReaderTests(unittest.TestCase):
    def test_reads_only_minimal_mcp_and_rate_limit_status(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "data": [
                            {
                                "name": "calendar",
                                "authStatus": "notLoggedIn",
                                "pluginId": None,
                                "serverInfo": None,
                                "tools": {"private": {"inputSchema": {"secret": True}}},
                                "resources": [],
                                "resourceTemplates": [],
                            }
                        ],
                        "nextCursor": None,
                    },
                },
                {
                    "id": 3,
                    "result": {
                        "rateLimits": {
                            "limitId": "codex",
                            "limitName": None,
                            "planType": None,
                            "primary": {
                                "usedPercent": 100,
                                "windowDurationMins": 60,
                                "resetsAt": 1234,
                            },
                            "secondary": None,
                            "credits": None,
                            "individualLimit": None,
                            "spendControlReached": None,
                            "rateLimitReachedType": "rate_limit_reached",
                        },
                        "rateLimitsByLimitId": None,
                        "rateLimitResetCredits": {
                            "availableCount": 1,
                            "credits": [{"id": "must-not-be-retained"}],
                        },
                    },
                },
            ]
        )
        snapshot = ExperimentalStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        ).read({"mcp-auth", "rate-limits"})

        self.assertEqual(
            snapshot.mcp_auth,
            (McpAuthObservation.from_name("calendar", "notLoggedIn"),),
        )
        self.assertEqual(len(snapshot.rate_limits or ()), 1)
        rate = snapshot.rate_limits[0]
        self.assertEqual(rate.reached_type, "rate_limit_reached")
        self.assertNotIn("must-not-be-retained", repr(snapshot))
        self.assertNotIn("inputSchema", repr(snapshot))
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(
            requests[2],
            {
                "id": 2,
                "method": "mcpServerStatus/list",
                "params": {"detail": "toolsAndAuthOnly", "limit": 50},
            },
        )
        self.assertEqual(
            requests[3], {"id": 3, "method": "account/rateLimits/read"}
        )
        self.assertTrue(process.terminated)

    def test_capability_probe_fails_closed_for_non_object_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "v2").mkdir()
            fixtures = {
                root / "v2" / "ConfigRequirementsReadResponse.json": {},
                root / "ToolRequestUserInputParams.json": [],
                root / "v2" / "ListMcpServerStatusParams.json": {},
                root / "v2" / "ListMcpServerStatusResponse.json": {},
                root / "v2" / "GetAccountRateLimitsResponse.json": {},
            }
            for path, payload in fixtures.items():
                path.write_text(json.dumps(payload), encoding="utf-8")
            capabilities = probe_experimental_capabilities(
                Path("/codex"),
                run=lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
                output_directory=root,
            )

        self.assertTrue(all(not item.available for item in capabilities.values()))

    def test_capability_probe_keeps_supported_features_when_one_schema_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "v2"
            generated.mkdir()
            (root / "ToolRequestUserInputParams.json").write_text(
                json.dumps({"title": "ToolRequestUserInputParams"}), encoding="utf-8"
            )
            (generated / "ConfigRequirementsReadResponse.json").write_text(
                json.dumps({"properties": {"PreToolUse": {"type": "array"}}}),
                encoding="utf-8",
            )
            (generated / "ListMcpServerStatusParams.json").write_text(
                json.dumps({"enum": ["toolsAndAuthOnly"]}), encoding="utf-8"
            )
            (generated / "ListMcpServerStatusResponse.json").write_text(
                json.dumps({"enum": ["notLoggedIn"]}), encoding="utf-8"
            )

            capabilities = probe_experimental_capabilities(
                Path("/codex"),
                run=lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
                output_directory=root,
            )

        self.assertTrue(capabilities["request-user-input"].available)
        self.assertTrue(capabilities["mcp-auth"].available)
        self.assertFalse(capabilities["rate-limits"].available)

    def test_query_failures_are_independent_and_unknown_fields_fail_closed(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": [], "unexpected": True}},
                {
                    "id": 3,
                    "result": {
                        "rateLimits": {"rateLimitReachedType": None},
                        "rateLimitsByLimitId": None,
                    },
                },
            ]
        )
        snapshot = ExperimentalStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        ).read({"mcp-auth", "rate-limits"})
        self.assertIsNone(snapshot.mcp_auth)
        self.assertEqual(snapshot.rate_limits, (RateLimitObservation.normal("default"),))

    def test_empty_multi_bucket_view_falls_back_to_required_single_bucket(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "rateLimits": {
                            "limitId": "codex",
                            "rateLimitReachedType": "rate_limit_reached",
                            "primary": {
                                "usedPercent": 100,
                                "windowDurationMins": 60,
                                "resetsAt": 1234,
                            },
                        },
                        "rateLimitsByLimitId": {},
                    },
                },
            ]
        )

        snapshot = ExperimentalStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        ).read({"rate-limits"})

        self.assertEqual(len(snapshot.rate_limits or ()), 1)
        self.assertEqual(snapshot.rate_limits[0].reached_type, "rate_limit_reached")

    def test_rate_limit_window_accepts_finite_float_percentage(self):
        process = _Process(
            [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "rateLimits": {
                            "rateLimitReachedType": "rate_limit_reached",
                            "primary": {
                                "usedPercent": 42.5,
                                "windowDurationMins": 60,
                                "resetsAt": 1234,
                            },
                        },
                        "rateLimitsByLimitId": None,
                    },
                },
            ]
        )

        snapshot = ExperimentalStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: process
        ).read({"rate-limits"})

        self.assertEqual(len(snapshot.rate_limits or ()), 1)
        self.assertEqual(snapshot.rate_limits[0].reached_type, "rate_limit_reached")

    def test_rate_limit_window_rejects_non_finite_percentage(self):
        for used_percent in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(used_percent=used_percent):
                process = _Process(
                    [
                        {"id": 1, "result": {}},
                        {
                            "id": 2,
                            "result": {
                                "rateLimits": {
                                    "rateLimitReachedType": "rate_limit_reached",
                                    "primary": {"usedPercent": used_percent},
                                },
                                "rateLimitsByLimitId": None,
                            },
                        },
                    ]
                )

                snapshot = ExperimentalStatusReader(
                    Path("/codex"), popen=lambda *args, **kwargs: process
                ).read({"rate-limits"})

                self.assertIsNone(snapshot.rate_limits)

    def test_repeated_cursor_and_oversized_collections_fail_closed(self):
        repeated = _Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"data": [], "nextCursor": "same"}},
                {"id": 3, "result": {"data": [], "nextCursor": "same"}},
            ]
        )
        snapshot = ExperimentalStatusReader(
            Path("/codex"), popen=lambda *args, **kwargs: repeated
        ).read({"mcp-auth"})
        self.assertIsNone(snapshot.mcp_auth)

    def test_schema_probe_requires_exact_safe_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated" / "v2"
            generated.mkdir(parents=True)
            (root / "generated" / "ToolRequestUserInputParams.json").write_text(
                json.dumps({"title": "ToolRequestUserInputParams"}), encoding="utf-8"
            )
            (generated / "ConfigRequirementsReadResponse.json").write_text(
                json.dumps(
                    {
                        "definitions": {
                            "ManagedHooksRequirements": {
                                "properties": {"PreToolUse": {"type": "array"}}
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (generated / "ListMcpServerStatusParams.json").write_text(
                json.dumps({"definitions": {"McpServerStatusDetail": {"enum": ["toolsAndAuthOnly"]}}}),
                encoding="utf-8",
            )
            (generated / "ListMcpServerStatusResponse.json").write_text(
                json.dumps({"definitions": {"McpAuthStatus": {"enum": ["notLoggedIn"]}}}),
                encoding="utf-8",
            )
            (generated / "GetAccountRateLimitsResponse.json").write_text(
                json.dumps(
                    {
                        "definitions": {
                            "RateLimitReachedType": {
                                "enum": [
                                    "rate_limit_reached",
                                    "workspace_owner_credits_depleted",
                                    "workspace_member_credits_depleted",
                                    "workspace_owner_usage_limit_reached",
                                    "workspace_member_usage_limit_reached",
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            class Result:
                returncode = 0

            def run(arguments, **kwargs):
                self.assertEqual(arguments[-1], str(root / "generated"))
                return Result()

            capabilities = probe_experimental_capabilities(
                Path("/codex"), run=run, output_directory=root / "generated"
            )
        self.assertTrue(capabilities["request-user-input"].available)
        self.assertTrue(capabilities["mcp-auth"].available)
        self.assertTrue(capabilities["rate-limits"].available)


class ExperimentalStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = NotificationStore(AppPaths(Path(self.temp_dir.name)))

    def _enable(self, feature):
        self.store.set_experimental_capability(feature, True, "fixture", now=1)
        self.store.set_experimental_enabled(feature, True, now=2)

    def test_features_default_off_and_unavailable_cannot_enable(self):
        status = self.store.experimental_feature_status()
        self.assertTrue(all(not item["enabled"] for item in status.values()))
        self.store.set_experimental_capability("mcp-auth", False, "unsafe", now=1)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.store.set_experimental_enabled("mcp-auth", True, now=2)

    def test_mcp_auth_notifies_on_transition_and_resets_after_recovery(self):
        self.store.set_enabled(True, now=1)
        self._enable("mcp-auth")
        not_logged_in = ExperimentalSnapshot(
            mcp_auth=(McpAuthObservation.from_name("calendar", "notLoggedIn"),)
        )
        healthy = ExperimentalSnapshot(
            mcp_auth=(McpAuthObservation.from_name("calendar", "oAuth"),)
        )
        self.assertEqual(self.store.record_experimental_snapshot(not_logged_in, now=10), 1)
        self.assertEqual(self.store.record_experimental_snapshot(not_logged_in, now=11), 0)
        self.assertEqual(self.store.record_experimental_snapshot(healthy, now=12), 0)
        self.assertEqual(self.store.record_experimental_snapshot(not_logged_in, now=13), 1)
        with self.store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT session_id, turn_id, event_type, payload_json FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["session_id"] is None and row["turn_id"] is None for row in rows))
        self.assertNotIn("tools", "".join(row["payload_json"] for row in rows))

    def test_mcp_auth_indeterminate_status_preserves_active_alert_episode(self):
        self.store.set_enabled(True, now=1)
        self._enable("mcp-auth")

        def snapshot(auth_status):
            return ExperimentalSnapshot(
                mcp_auth=(McpAuthObservation.from_name("calendar", auth_status),)
            )

        self.assertEqual(
            self.store.record_experimental_snapshot(snapshot("notLoggedIn"), now=10),
            1,
        )
        self.assertEqual(
            self.store.record_experimental_snapshot(snapshot("unknown"), now=11), 0
        )
        self.assertEqual(
            self.store.record_experimental_snapshot(snapshot("unsupported"), now=12),
            0,
        )
        self.assertEqual(
            self.store.record_experimental_snapshot(snapshot("notLoggedIn"), now=13),
            0,
        )

        with self.store.managed_connection() as connection:
            state = connection.execute(
                "SELECT cooldown_key, last_notified_cooldown_key "
                "FROM experimental_signal_state WHERE feature='mcp-auth'"
            ).fetchone()
            alert_count = connection.execute(
                "SELECT COUNT(*) AS count FROM outbox "
                "WHERE event_type='experimental_mcp_auth'"
            ).fetchone()["count"]
        self.assertEqual(alert_count, 1)
        self.assertEqual(state["cooldown_key"], state["last_notified_cooldown_key"])

    def test_mcp_auth_service_removal_resets_state_without_reusing_event_key(self):
        self.store.set_enabled(True, now=1)
        self._enable("mcp-auth")
        not_logged_in = ExperimentalSnapshot(
            mcp_auth=(McpAuthObservation.from_name("calendar", "notLoggedIn"),)
        )

        self.assertEqual(self.store.record_experimental_snapshot(not_logged_in, now=10), 1)
        self.assertEqual(
            self.store.record_experimental_snapshot(
                ExperimentalSnapshot(mcp_auth=()), now=11
            ),
            0,
        )
        self.assertEqual(self.store.record_experimental_snapshot(not_logged_in, now=12), 1)

        with self.store.managed_connection() as connection:
            state = connection.execute(
                "SELECT last_state, transition_count FROM experimental_signal_state "
                "WHERE feature='mcp-auth'"
            ).fetchone()
            event_keys = [
                row["event_key"]
                for row in connection.execute(
                    "SELECT event_key FROM outbox "
                    "WHERE event_type='experimental_mcp_auth' ORDER BY id"
                )
            ]
        self.assertEqual(state["last_state"], "notLoggedIn")
        self.assertEqual(state["transition_count"], 3)
        self.assertEqual(len(event_keys), 2)
        self.assertEqual(len(set(event_keys)), 2)

    def test_rate_limit_same_window_only_once_even_after_recovery(self):
        self.store.set_enabled(True, now=1)
        self._enable("rate-limits")
        reached = RateLimitObservation(
            "bucket-hash", "rate_limit_reached", "window-one"
        )
        normal = RateLimitObservation("bucket-hash", None, "window-one-normal")
        next_window = RateLimitObservation(
            "bucket-hash", "rate_limit_reached", "window-two"
        )
        self.assertEqual(
            self.store.record_experimental_snapshot(
                ExperimentalSnapshot(rate_limits=(reached,)), now=10
            ),
            1,
        )
        self.store.record_experimental_snapshot(
            ExperimentalSnapshot(rate_limits=(normal,)), now=11
        )
        self.assertEqual(
            self.store.record_experimental_snapshot(
                ExperimentalSnapshot(rate_limits=(reached,)), now=12
            ),
            0,
        )
        self.assertEqual(
            self.store.record_experimental_snapshot(
                ExperimentalSnapshot(rate_limits=(next_window,)), now=13
            ),
            1,
        )

    def test_off_now_preserves_recovered_rate_limit_window_deduplication(self):
        self.store.set_enabled(True, now=1)
        self._enable("rate-limits")
        reached = RateLimitObservation(
            "bucket-hash", "rate_limit_reached", "window-one"
        )
        normal = RateLimitObservation("bucket-hash", None, "window-one-normal")

        self.assertEqual(
            self.store.record_experimental_snapshot(
                ExperimentalSnapshot(rate_limits=(reached,)), now=10
            ),
            1,
        )
        self.store.record_experimental_snapshot(
            ExperimentalSnapshot(rate_limits=(normal,)), now=11
        )
        self.store.set_enabled(False, immediate=True, now=12)
        self.store.set_enabled(True, now=13)

        self.assertEqual(
            self.store.record_experimental_snapshot(
                ExperimentalSnapshot(rate_limits=(reached,)), now=14
            ),
            0,
        )

    def test_query_failure_does_not_overwrite_state_and_attempt_is_rate_limited(self):
        self.store.set_enabled(True, now=1)
        self._enable("mcp-auth")
        self.assertEqual(self.store.experimental_query_features(now=10), {"mcp-auth"})
        self.store.mark_experimental_query_attempt(now=10)
        self.assertEqual(self.store.experimental_query_features(now=20), set())
        self.store.record_experimental_snapshot(
            ExperimentalSnapshot(
                mcp_auth=(McpAuthObservation.from_name("calendar", "notLoggedIn"),)
            ),
            now=80,
        )
        self.store.record_experimental_snapshot(ExperimentalSnapshot(), now=90)
        with self.store.managed_connection() as connection:
            row = connection.execute(
                "SELECT last_state FROM experimental_signal_state"
            ).fetchone()
        self.assertEqual(row["last_state"], "notLoggedIn")

    def test_off_now_permanently_suppresses_current_experimental_state(self):
        self.store.set_enabled(True, now=1)
        self._enable("mcp-auth")
        snapshot = ExperimentalSnapshot(
            mcp_auth=(McpAuthObservation.from_name("calendar", "notLoggedIn"),)
        )
        self.store.record_experimental_snapshot(snapshot, now=10)
        self.store.set_enabled(False, immediate=True, now=11)
        self.store.set_enabled(True, now=12)
        self.assertEqual(self.store.record_experimental_snapshot(snapshot, now=13), 0)
        with self.store.managed_connection() as connection:
            status = connection.execute("SELECT status FROM outbox").fetchone()["status"]
        self.assertEqual(status, "suppressed")

    def test_disabling_one_feature_suppresses_only_its_queued_alerts(self):
        self.store.set_enabled(True, now=1)
        for feature in ("mcp-auth", "rate-limits"):
            self._enable(feature)
        snapshot = ExperimentalSnapshot(
            mcp_auth=(McpAuthObservation.from_name("calendar", "notLoggedIn"),),
            rate_limits=(
                RateLimitObservation(
                    "bucket", "rate_limit_reached", "current-window"
                ),
            ),
        )
        self.assertEqual(self.store.record_experimental_snapshot(snapshot, now=10), 2)

        self.store.set_experimental_enabled("mcp-auth", False, now=11)

        with self.store.managed_connection() as connection:
            rows = {
                row["event_type"]: row["status"]
                for row in connection.execute(
                    "SELECT event_type, status FROM outbox ORDER BY id"
                )
            }
        self.assertEqual(rows["experimental_mcp_auth"], "suppressed")
        self.assertEqual(rows["experimental_rate_limit"], "pending")

    def test_disabling_feature_invalidates_an_already_claimed_alert(self):
        self.store.set_enabled(True, now=1)
        self._enable("mcp-auth")
        snapshot = ExperimentalSnapshot(
            mcp_auth=(McpAuthObservation.from_name("calendar", "notLoggedIn"),)
        )
        self.store.record_experimental_snapshot(snapshot, now=10)
        claimed = self.store.claim_due(limit=1, now=10)
        self.assertEqual(claimed[0]["event_type"], "experimental_mcp_auth")

        self.store.set_experimental_enabled("mcp-auth", False, now=11)

        self.assertFalse(self.store.is_sendable(claimed[0]["id"]))
        self.assertEqual(self.store.event_status(claimed[0]["event_key"]), "suppressed")


if __name__ == "__main__":
    unittest.main()
