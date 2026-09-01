import unittest

from entropy_mm.microstructure import depth_signal, distance_from_touch_bps, queue_ahead_size


class MicrostructureTests(unittest.TestCase):
    def setUp(self):
        self.levels = [
            [
                {"px": "100.0", "sz": "5"},
                {"px": "99.9", "sz": "4"},
                {"px": "99.8", "sz": "3"},
            ],
            [
                {"px": "100.1", "sz": "1"},
                {"px": "100.2", "sz": "1"},
                {"px": "100.3", "sz": "1"},
            ],
        ]

    def test_depth_fair_moves_toward_heavier_bid_depth_but_stays_inside_bbo(self):
        signal = depth_signal(self.levels, depth_levels=3)
        self.assertGreater(signal.fair_value, 100.05)
        self.assertLessEqual(signal.fair_value, 100.1)
        self.assertGreater(signal.imbalance, 0)

    def test_queue_ahead_counts_better_and_same_price_liquidity(self):
        self.assertEqual(queue_ahead_size(self.levels, "buy", 99.9), 9.0)
        self.assertEqual(queue_ahead_size(self.levels, "sell", 100.2), 2.0)

    def test_touch_distance_is_side_aware(self):
        self.assertAlmostEqual(distance_from_touch_bps(100.0, 100.1, "buy", 99.9), 9.9950024987, places=5)
        self.assertAlmostEqual(distance_from_touch_bps(100.0, 100.1, "sell", 100.2), 9.9950024987, places=5)


if __name__ == "__main__":
    unittest.main()
