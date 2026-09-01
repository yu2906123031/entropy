import json
import tempfile
import threading
import unittest
from pathlib import Path

from entropy_mm.lifecycle import AlreadyRunning, LoopConfig, ProcessLock, run_loop


class LifecycleTests(unittest.TestCase):
    def test_successful_cycles_write_health_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            health_path = Path(tmp) / "health.json"
            calls = []
            health = run_loop(
                lambda: calls.append("cycle"),
                lambda: calls.append("shutdown"),
                health_path=health_path,
                config=LoopConfig(interval_seconds=0),
                max_cycles=2,
                sleep=lambda _: None,
            )
            payload = json.loads(health_path.read_text())
            self.assertEqual(calls, ["cycle", "cycle", "shutdown"])
            self.assertEqual(health.completed_cycles, 2)
            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["stop_reason"], "max_cycles")

    def test_failures_back_off_and_stop_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []

            def fail():
                raise TimeoutError("cycle timed out")

            health = run_loop(
                fail,
                lambda: None,
                health_path=Path(tmp) / "health.json",
                config=LoopConfig(
                    interval_seconds=0,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=10,
                    max_consecutive_failures=3,
                ),
                sleep=sleeps.append,
            )
            self.assertEqual(sleeps, [1, 2])
            self.assertEqual(health.consecutive_failures, 3)
            self.assertEqual(health.stop_reason, "failure_limit")
            self.assertIn("TimeoutError", health.last_error)

    def test_stop_event_prevents_cycle_and_still_runs_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = threading.Event()
            stop.set()
            calls = []
            health = run_loop(
                lambda: calls.append("cycle"),
                lambda: calls.append("shutdown"),
                health_path=Path(tmp) / "health.json",
                stop_event=stop,
            )
            self.assertEqual(calls, ["shutdown"])
            self.assertEqual(health.stop_reason, "requested")

    def test_shutdown_error_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            def bad_shutdown():
                raise RuntimeError("cancel failed")

            health = run_loop(
                lambda: None,
                bad_shutdown,
                health_path=Path(tmp) / "health.json",
                max_cycles=1,
            )
            self.assertEqual(health.stop_reason, "shutdown_error")
            self.assertIn("cancel failed", health.last_error)

    def test_process_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daemon.lock"
            with ProcessLock(path):
                with self.assertRaises(AlreadyRunning):
                    with ProcessLock(path):
                        pass
            with ProcessLock(path):
                self.assertTrue(path.read_text().strip())


if __name__ == "__main__":
    unittest.main()
