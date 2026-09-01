import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from entropy_mm.closed_loop import (
    cancel_opening_orders_on_shutdown,
    capture_orders,
    check_position,
    finalize_cycle,
)
from entropy_mm.execution import ActionResult, ExecutionMode, ExecutionResult
from entropy_mm.operations import OperationsStore, assess_window
from entropy_mm.quote_model import Inventory
from scripts.mm_live_preflight import ABSOLUTE_PROBE_CAP_USD, build_report


class TrapVenue:
    def cancel_orders(self, order_ids):
        raise AssertionError("venue called")

    def place_orders(self, places):
        raise AssertionError("venue called")

    def open_order_ids(self):
        raise AssertionError("venue called")


class ClosedLoopTests(unittest.TestCase):
    def test_window_metrics_report_latency_survival_and_churn(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OperationsStore(Path(tmp) / "ops.sqlite3")
            store.record_cycle_sample(
                succeeded=True, latency_ms=100, kept_quotes=3, observed_quotes=4,
                planned_cancels=1, planned_places=2, created_at_ms=1_000,
            )
            store.record_cycle_sample(
                succeeded=False, latency_ms=300, kept_quotes=1, observed_quotes=2,
                planned_cancels=2, planned_places=1, created_at_ms=61_000,
            )
            metrics = store.window_metrics(last_n=2)
            self.assertEqual(metrics.sample_count, 2)
            self.assertEqual(metrics.successful_cycles, 1)
            self.assertEqual(metrics.latency_p50_ms, 100)
            self.assertEqual(metrics.latency_p95_ms, 300)
            self.assertAlmostEqual(metrics.quote_survival_rate, 4 / 6)
            self.assertEqual(metrics.planned_order_actions, 6)
            self.assertEqual(metrics.accepted_order_actions, 0)
            self.assertIsNotNone(metrics.planned_order_actions_per_minute)
            self.assertAlmostEqual(metrics.planned_order_actions_per_minute or 0.0, 6 / (60_100 / 60_000))

    def test_window_metrics_separate_dry_run_and_live_and_mark_survival_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OperationsStore(Path(tmp) / "ops.sqlite3")
            store.record_cycle_sample(
                succeeded=True, latency_ms=1_000, kept_quotes=0, observed_quotes=0,
                planned_cancels=0, planned_places=6, mode="dry_run", created_at_ms=2_000,
            )
            store.record_cycle_sample(
                succeeded=True, latency_ms=1_000, kept_quotes=1, observed_quotes=2,
                planned_cancels=1, planned_places=1, mode="live",
                accepted_cancels=1, accepted_places=1, created_at_ms=4_000,
            )
            dry = store.window_metrics(mode="dry_run")
            live = store.window_metrics(mode="live")
            self.assertIsNone(dry.quote_survival_rate)
            self.assertEqual(dry.accepted_order_actions, 0)
            self.assertEqual(live.quote_survival_rate, 0.5)
            self.assertEqual(live.accepted_order_actions, 2)
            assessment = assess_window(dry, min_samples=1, max_planned_actions_per_minute=1_000)
            self.assertTrue(assessment.ready)

    def test_live_preflight_caps_probe_and_runtime_verifies_daemon_lock(self):
        env = {
            "ENTROPY_ACCOUNT": "",
            "ENTROPY_OPERATOR_ADDRESS": "",
            "ENTROPY_LIVE_PROBE_MAX_USD": str(ABSOLUTE_PROBE_CAP_USD + 1),
        }
        with patch.dict("os.environ", env, clear=True):
            report = build_report()
        self.assertFalse(report["ready_for_explicit_write_probe_approval"])
        self.assertFalse(report["checks"]["probe_cap_valid"])
        self.assertTrue(report["checks"]["live_daemon_lock_runtime_verified"])
        self.assertEqual(report["signed_calls"], 0)

    def test_execution_is_persisted_and_accepted_orders_become_managed(self):
        with tempfile.TemporaryDirectory() as folder:
            store = OperationsStore(Path(folder) / "ops.sqlite3")
            execution = ExecutionResult(
                ExecutionMode.LIVE,
                "executed",
                (ActionResult("4", True, "success", 4),),
                (ActionResult("buy:0", True, "resting", 9),),
            )
            store.record_execution(execution, created_at_ms=100)
            self.assertEqual(store.execution_count(), 1)
            self.assertEqual(store.active_managed_order_ids(), {9})

    def test_snapshot_identifies_only_unknown_opening_orders_as_orphans(self):
        rows = [
            {"coin": "HYPE", "oid": 9, "reduceOnly": False},
            {"coin": "HYPE", "oid": 10, "reduceOnly": False},
            {"coin": "HYPE", "oid": 11, "reduceOnly": True},
            {"coin": "BTC", "oid": 12, "reduceOnly": False},
        ]
        snapshot = capture_orders(rows, {9}, coin="HYPE", captured_at_ms=20)
        self.assertEqual(snapshot.opening_order_ids, (9, 10))
        self.assertEqual(snapshot.reduce_only_order_ids, (11,))
        self.assertEqual(snapshot.orphan_order_ids, (10,))

    def test_finalize_updates_long_run_metrics_and_position_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            store = OperationsStore(Path(folder) / "ops.sqlite3")
            execution = ExecutionResult(ExecutionMode.DRY_RUN, "planned_only", (), ())
            snapshot = capture_orders([], set(), coin="HYPE", captured_at_ms=30)
            position = check_position(Inventory(long=1), Inventory(long=0))
            closure = finalize_cycle(
                store, execution, snapshot, position,
                planned_cancels=2, planned_places=3, synced_fills=1,
                cycle_succeeded=False, now_ms=40,
            )
            self.assertEqual(closure.metrics.cycles, 1)
            self.assertEqual(closure.metrics.failed_cycles, 1)
            self.assertEqual(closure.metrics.position_mismatches, 1)
            self.assertEqual(closure.metrics.planned_places, 3)

    def test_shutdown_dry_run_preserves_reduce_only_and_makes_no_calls(self):
        result = cancel_opening_orders_on_shutdown(
            TrapVenue(),
            [
                {"coin": "HYPE", "oid": 1, "reduceOnly": False},
                {"coin": "HYPE", "oid": 2, "reduceOnly": True},
            ],
            coin="HYPE",
            mode=ExecutionMode.DRY_RUN,
            live_enabled=False,
            confirmation="",
        )
        self.assertEqual(result.status, "planned_only")

    def test_shutdown_live_cancels_only_opening_orders(self):
        class Venue:
            cancelled = ()
            def cancel_orders(self, ids):
                self.cancelled = ids
                return tuple(ActionResult(str(oid), True, "success", oid) for oid in ids)
            def place_orders(self, places):
                raise AssertionError("place called")
            def open_order_ids(self):
                return {2}
        venue = Venue()
        result = cancel_opening_orders_on_shutdown(
            venue,
            [
                {"coin": "HYPE", "oid": 1, "reduceOnly": False},
                {"coin": "HYPE", "oid": 2, "reduceOnly": True},
            ],
            coin="HYPE",
            mode=ExecutionMode.LIVE,
            live_enabled=True,
            confirmation="ENTROPY_LIVE_ORDER_EXECUTION",
        )
        self.assertEqual(venue.cancelled, (1,))
        self.assertEqual(result.status, "cancelled_only")


if __name__ == "__main__":
    unittest.main()
