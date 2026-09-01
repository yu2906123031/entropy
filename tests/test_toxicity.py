import unittest

from entropy_mm.toxicity import FillObservation, MarkoutTracker, signed_markout_bps


class ToxicityTests(unittest.TestCase):
    def test_buy_markout_is_positive_when_price_rises(self):
        self.assertAlmostEqual(signed_markout_bps("buy", 100.0, 100.1), 10.0, places=6)

    def test_sell_markout_is_positive_when_price_falls(self):
        self.assertAlmostEqual(signed_markout_bps("sell", 100.0, 99.9), 10.0, places=6)

    def test_tracker_emits_each_horizon_once(self):
        tracker = MarkoutTracker(horizons_ms=(1000, 5000))
        tracker.add_fill(FillObservation("t1", "buy", 100.0, 0.2, 0))
        self.assertEqual(len(tracker.observe(999, 99.9)), 0)
        self.assertEqual(len(tracker.observe(1000, 99.9)), 1)
        self.assertEqual(len(tracker.observe(2000, 99.8)), 0)
        self.assertEqual(len(tracker.observe(5000, 99.7)), 1)
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


if __name__ == "__main__":
    unittest.main()
