import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path

from codex_notify.db import HookEvent, NotificationStore
from codex_notify.feishu import DeliveryError
from codex_notify.paths import AppPaths
from codex_notify.worker import run_once


class _Client:
    def __init__(self, error=None):
        self.error = error
        self.messages = []

    def send_text(self, message):
        self.messages.append(message)
        if self.error:
            raise self.error


class _BlockingClient:
    def __init__(self, started, release):
        self.started = started
        self.release = release

    def send_text(self, message):
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release sender")


def _run_blocking_worker(root, started, release):
    store = NotificationStore(AppPaths(Path(root)))
    client = _BlockingClient(started, release)
    run_once(store, client_factory=lambda: client, now=100)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = NotificationStore(AppPaths(Path(self.temp_dir.name)))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_marks_successful_delivery_sent(self):
        key = self.store.enqueue_test(now=100)
        client = _Client()
        self.assertEqual(run_once(self.store, client_factory=lambda: client, now=100), 1)
        self.assertEqual(self.store.event_status(key), "sent")
        self.assertIn("测试成功", client.messages[0])
        self.assertIn("事件：", client.messages[0])

    def test_worker_sends_standalone_completion_when_enabled_mid_turn(self):
        event = HookEvent(
            session_id="session-1",
            turn_id="turn-1",
            cwd="/work/example",
            prompt="Implement the feature",
        )
        self.assertFalse(self.store.record_start(event, now=100))
        self.store.set_enabled(True, now=101)
        completion = HookEvent(
            session_id="session-1",
            turn_id="turn-1",
            cwd="/work/example",
            last_assistant_message="Done",
        )
        self.assertFalse(self.store.record_completion(completion, now=120))
        client = _Client()

        self.assertEqual(run_once(self.store, client_factory=lambda: client, now=120), 1)

        self.assertEqual(len(client.messages), 1)
        self.assertIn("Codex Turn 结束", client.messages[0])
        self.assertIn("未观测到对应启动事件", client.messages[0])
        with self.store.managed_connection() as connection:
            rows = connection.execute(
                "SELECT event_type, status FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [(row["event_type"], row["status"]) for row in rows],
            [("completed", "sent")],
        )

    def test_worker_can_target_new_test_without_draining_old_batch(self):
        old_keys = [self.store.enqueue_test(now=100 + index) for index in range(20)]
        target = self.store.enqueue_test(now=200)
        client = _Client()

        self.assertEqual(
            run_once(
                self.store,
                client_factory=lambda: client,
                now=300,
                event_key=target,
            ),
            1,
        )

        self.assertEqual(self.store.event_status(target), "sent")
        self.assertTrue(all(self.store.event_status(key) == "pending" for key in old_keys))
        self.assertEqual(len(client.messages), 1)

    def test_retries_network_failure(self):
        key = self.store.enqueue_test(now=100)
        client = _Client(DeliveryError("offline", retryable=True))
        self.assertEqual(run_once(self.store, client_factory=lambda: client, now=100), 0)
        self.assertEqual(self.store.event_status(key), "retry")

    def test_permanent_rejection_goes_dead(self):
        key = self.store.enqueue_test(now=100)
        client = _Client(DeliveryError("bad signature", retryable=False))
        run_once(self.store, client_factory=lambda: client, now=100)
        self.assertEqual(self.store.event_status(key), "dead")

    def test_immediate_off_waits_for_inflight_send_then_pauses_delivery(self):
        self.store.set_enabled(True, now=90)
        key = self.store.enqueue_test(now=100)
        context = multiprocessing.get_context("fork")
        started = context.Event()
        release = context.Event()
        process = context.Process(
            target=_run_blocking_worker,
            args=(str(self.store.paths.root), started, release),
        )
        process.start()
        try:
            self.assertTrue(started.wait(2), "worker never entered send_text")
            off_finished = threading.Event()

            def turn_off():
                self.store.set_enabled(False, immediate=True, now=101)
                off_finished.set()

            thread = threading.Thread(target=turn_off)
            thread.start()
            self.assertFalse(off_finished.wait(0.1), "off --now did not wait for inflight send")
            release.set()
            thread.join(2)
            self.assertTrue(off_finished.is_set())
            process.join(2)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(self.store.event_status(key), "sent")
            self.assertTrue(self.store.is_delivery_paused())
        finally:
            release.set()
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)

    def test_explicit_test_bypasses_immediate_pause(self):
        self.store.set_enabled(True, now=90)
        self.store.set_enabled(False, immediate=True, now=91)
        key = self.store.enqueue_test(now=100)
        client = _Client()
        self.assertEqual(run_once(self.store, client_factory=lambda: client, now=100), 1)
        self.assertEqual(self.store.event_status(key), "sent")
        self.assertFalse(self.store.is_enabled())
        self.assertTrue(self.store.is_delivery_paused())


if __name__ == "__main__":
    unittest.main()
