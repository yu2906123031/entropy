import unittest

from entropy_mm.adaptive_strategy import adaptive_quote_pair
from entropy_mm.edge import EdgeDecision


class AdaptiveStrategyTests(unittest.TestCase):
    def test_shared_quote_builder_respects_edge_and_size(self):
        edge = EdgeDecision(20.0, 0.6, 3.0, 5.0, False, False, 4.0)
        pair = adaptive_quote_pair(
            best_bid=99.9,
            best_ask=100.1,
            fair_value=100.0,
            inventory=0.0,
            max_inventory=0.02,
            volatility_bps=3.0,
            bid_buffer_bps=2.0,
            ask_buffer_bps=2.0,
            edge=edge,
            base_half_spread_bps=10.0,
            round_trip_fee_bps=3.0,
            minimum_profit_bps=8.0,
            base_size=0.05,
            lot_size=0.01,
        )
        self.assertLessEqual(pair.bid, 99.9)
        self.assertGreaterEqual(pair.ask, 100.1)
        self.assertEqual(pair.size, 0.03)
        self.assertGreaterEqual(pair.half_spread_bps, 20.0)

    def test_long_inventory_moves_quotes_toward_flattening(self):
        edge = EdgeDecision(10.0, 1.0, 0.0, 0.0, False, False, 0.0)
        flat = adaptive_quote_pair(best_bid=99.9, best_ask=100.1, fair_value=100, inventory=0, max_inventory=0.02, volatility_bps=0, bid_buffer_bps=0, ask_buffer_bps=0, edge=edge, base_half_spread_bps=10, round_trip_fee_bps=3, minimum_profit_bps=8, base_size=0.02, lot_size=0.01)
        long = adaptive_quote_pair(best_bid=99.9, best_ask=100.1, fair_value=100, inventory=0.02, max_inventory=0.02, volatility_bps=0, bid_buffer_bps=0, ask_buffer_bps=0, edge=edge, base_half_spread_bps=10, round_trip_fee_bps=3, minimum_profit_bps=8, base_size=0.02, lot_size=0.01)
        self.assertLess(long.bid, flat.bid)
        self.assertLess(long.ask, flat.ask)


if __name__ == "__main__":
    unittest.main()
