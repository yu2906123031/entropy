import tempfile
import unittest
from pathlib import Path

from entropy_mm.toxicity import FillObservation, MarkoutStore, MarkoutTracker, signed_markout_bps


class ToxicityTests(unittest.TestCase):
    def test_buy_markout_is_positive_when_price_rises(self):
        self.assertAlmostEqual(signed_markout_bps("buy", 100.0, 100.1), 10.0, places=6)

    def test_sell_markout_is_positive_when_price_falls(self):
        self.assertAlmostEqual(signed_markout_bps("sell", 100.0, 99.9), 10.0, places=6)

    def test_tracker_emits_each_horizon_once(self):
        tracker = MarkoutTracker(horizons_ms=(1000, 5000))
        tracker.add_fill(FillObservation("t1", "buy", 100.0, 0.2, 0))
        self.assertEqual(len(tracker.observe(999, 99.9)), 0)
        first = tracker.observe(1000, 99.9)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(tracker.observe(2000, 99.8)), 0)
        second = tracker.observe(5000, 99.7)
        self.assertEqual(len(second), 1)
        self.assertNotIn("t1", tracker.pending)

    def test_negative_mean_flags_toxic_flow_after_minimum_samples(self):
        tracker = MarkoutTracker(horizons_ms=(1000,))
        for i in range(10):
            tracker.add_fill(FillObservation(f"t{i}", "buy", 100.0, 0.1, i))
        tracker.observe(2000, 99.9)
        summary = tracker.summary(1000, toxic_mean_bps=-2.0, min_samples=10)
        self.assertTrue(summary.toxic)
        self.assertLess(summary.mean_markout_bps, 0)
        self.assertEqual(summary.negative_rate, 1.0)

    def test_store_is_restart_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "markouts.sqlite3"
            tracker = MarkoutTracker(horizons_ms=(5000,))
            tracker.add_fill(FillObservation("t1", "buy", 100.0, 0.1, 0))
            produced = tracker.observe(5000, 99.9)
            first = MarkoutStore(path)
            self.assertEqual(first.record(produced, recorded_at_ms=5000), 1)
            self.assertEqual(first.record(produced, recorded_at_ms=6000), 0)
            restarted = MarkoutStore(path)
            summary = restarted.summary(5000, last_n=10, min_samples=1, toxic_mean_bps=-2.0)
            self.assertEqual(summary.count, 1)
            self.assertTrue(summary.toxic)


if __name__ == "__main__":
    unittest.main()
