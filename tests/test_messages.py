import unittest

from codex_notify.messages import render_message


class ExperimentalMessageTests(unittest.TestCase):
    def payload(self, **updates):
        value = {
            "certainty": "best_effort",
            "signal_source": "app_server_status",
            "signal_kind": "test",
            "observed_at": 1,
            "occurred_at": 1,
            "signal_id": "safe-signal",
            "event_id": "safe-event",
        }
        value.update(updates)
        return value

    def test_request_user_input_copy_is_best_effort_and_turn_scoped(self):
        message = render_message(
            "experimental_request_user_input",
            self.payload(
                signal_source="hook",
                project="demo",
                turn_id="turn-1234567890",
            ),
        )
        self.assertIn("可能需要你回答一个问题", message)
        self.assertIn("建议回到 Codex 检查", message)
        self.assertIn("可信度：best_effort", message)
        self.assertIn("信号来源：hook", message)
        self.assertIn("Turn：turn-123456", message)

    def test_global_messages_do_not_fabricate_turn_id(self):
        for event_type, text in (
            ("experimental_mcp_auth", "可能需要重新登录"),
            ("experimental_rate_limit", "可能达到限流"),
        ):
            message = render_message(
                event_type,
                self.payload(display_name="calendar"),
            )
            self.assertIn("全局状态提醒", message)
            self.assertIn(text, message)
            self.assertNotIn("Turn：", message)
            self.assertIn("信号：safe-signal", message)

    def test_defensive_legacy_permission_copy_does_not_claim_pending_approval(self):
        message = render_message(
            "permission",
            self.payload(
                project="demo",
                turn_id="turn-1234567890",
                summary="命令执行审批",
            ),
        )
        self.assertIn("权限检查记录", message)
        self.assertIn("不代表当前仍在等待人工审批", message)
        self.assertNotIn("Codex 需要审批", message)

    def test_legacy_queued_payload_is_resanitized_at_delivery(self):
        local_path = "/" + "Users/alice/private/report.md"
        message = render_message(
            "completed",
            self.payload(
                project="demo",
                turn_id="turn-1234567890",
                terminal_status="completed",
                duration_seconds=1,
                summary=f"任务完成，输出 {local_path}",
                child_results=[
                    {"agent_type": "worker", "summary": f"读取 {local_path}"}
                ],
            ),
        )

        self.assertNotIn(local_path, message)
        self.assertEqual(message.count("[本地路径已打码]"), 2)
        self.assertIn("任务完成", message)
        self.assertIn("子任务结果", message)


if __name__ == "__main__":
    unittest.main()
