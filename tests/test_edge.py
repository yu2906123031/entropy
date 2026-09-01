import unittest

from entropy_mm.edge import directional_pressure_bps, dynamic_order_size, profitability_edge


class EdgeTests(unittest.TestCase):
    def test_directional_pressure_is_signed(self):
        self.assertGreater(directional_pressure_bps([100, 100.1, 100.2]), 0)
        self.assertLess(directional_pressure_bps([100, 99.9, 99.8]), 0)

    def test_adverse_markout_raises_required_edge_and_reduces_size(self):
        calm = profitability_edge(
            volatility_bps=2,
            book_imbalance=0,
            markout_mean_bps=1,
            markout_negative_rate=0.3,
            directional_bps=0,
            round_trip_fee_bps=3,
            minimum_profit_bps=8,
        )
        toxic = profitability_edge(
            volatility_bps=8,
            book_imbalance=0.7,
            markout_mean_bps=-6,
            markout_negative_rate=0.8,
            directional_bps=15,
            round_trip_fee_bps=3,
            minimum_profit_bps=8,
        )
        self.assertGreater(toxic.required_edge_bps, calm.required_edge_bps)
        self.assertLess(toxic.size_multiplier, calm.size_multiplier)

    def test_upward_direction_penalizes_ask(self):
        decision = profitability_edge(
            volatility_bps=4,
            book_imbalance=0.2,
            markout_mean_bps=0,
            markout_negative_rate=0.4,
            directional_bps=20,
            round_trip_fee_bps=3,
            minimum_profit_bps=8,
        )
        self.assertGreater(decision.ask_extra_bps, decision.bid_extra_bps)

    def test_strong_direction_pauses_adverse_side(self):
        up = profitability_edge(volatility_bps=6, book_imbalance=0, markout_mean_bps=0, markout_negative_rate=0.5, directional_bps=35, round_trip_fee_bps=3, minimum_profit_bps=8)
        down = profitability_edge(volatility_bps=6, book_imbalance=0, markout_mean_bps=0, markout_negative_rate=0.5, directional_bps=-35, round_trip_fee_bps=3, minimum_profit_bps=8)
        self.assertTrue(up.pause_ask)
        self.assertTrue(down.pause_bid)

    def test_dynamic_order_size_quantizes_down(self):
        self.assertAlmostEqual(dynamic_order_size(0.05, 0.01, 0.65), 0.03)
        self.assertEqual(dynamic_order_size(0.02, 0.01, 0.20), 0.0)


if __name__ == "__main__":
    unittest.main()
