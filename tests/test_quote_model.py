import math
import unittest

from entropy_mm.quote_model import (
    Book,
    Inventory,
    QuoteConfig,
    RiskLimits,
    build_quotes,
    microprice,
    opening_capacities,
)


class QuoteModelTests(unittest.TestCase):
    def setUp(self):
        self.book = Book(bid=99.9, ask=100.1, bid_size=8, ask_size=2)
        self.limits = RiskLimits(max_long=1.0, max_short=1.0, max_net=0.6, max_gross=1.2)
        self.cfg = QuoteConfig(tick_size=0.1, lot_size=0.01, order_size=0.2)

    def test_microprice_weights_toward_thinner_ask(self):
        self.assertAlmostEqual(microprice(self.book), 100.06)

    def test_flat_quotes_are_dual_sided_post_only_and_directionally_quantized(self):
        quotes = build_quotes(self.book, Inventory(), self.limits, self.cfg)
        bids = [q for q in quotes if q.side == "buy"]
        asks = [q for q in quotes if q.side == "sell"]
        self.assertEqual(len(bids), 3)
        self.assertEqual(len(asks), 3)
        self.assertTrue(all(q.price <= self.book.bid for q in bids))
        self.assertTrue(all(q.price >= self.book.ask for q in asks))
        self.assertTrue(all(math.isclose(q.price / 0.1, round(q.price / 0.1), abs_tol=1e-9) for q in quotes))

    def test_long_inventory_shifts_quotes_down(self):
        flat = build_quotes(self.book, Inventory(), self.limits, self.cfg)
        long = build_quotes(self.book, Inventory(long=0.4), self.limits, self.cfg)
        flat_ask = next(q.price for q in flat if q.side == "sell")
        long_ask = next(q.price for q in long if q.side == "sell")
        self.assertLess(long_ask, flat_ask)

    def test_shared_gross_budget_reserves_capacity_for_both_sides(self):
        long_cap, short_cap = opening_capacities(Inventory(), self.limits)
        self.assertEqual((long_cap, short_cap), (0.6, 0.6))
        quotes = build_quotes(self.book, Inventory(), self.limits, self.cfg)
        self.assertLessEqual(sum(q.size for q in quotes), self.limits.max_gross)

    def test_net_limit_suppresses_risk_increasing_side(self):
        quotes = build_quotes(self.book, Inventory(long=0.6), self.limits, self.cfg)
        self.assertFalse(any(q.side == "buy" for q in quotes))
        self.assertTrue(any(q.side == "sell" for q in quotes))

    def test_elevated_volatility_widens_reduces_layers_and_size(self):
        normal = build_quotes(self.book, Inventory(), self.limits, self.cfg, volatility_bps=10)
        elevated = build_quotes(self.book, Inventory(), self.limits, self.cfg, volatility_bps=25)
        self.assertEqual(len(normal), 6)
        self.assertEqual(len(elevated), 4)
        self.assertTrue(all(q.size == 0.1 for q in elevated))
        normal_bid = next(q.price for q in normal if q.side == "buy")
        normal_ask = next(q.price for q in normal if q.side == "sell")
        elevated_bid = next(q.price for q in elevated if q.side == "buy")
        elevated_ask = next(q.price for q in elevated if q.side == "sell")
        self.assertGreater(elevated_ask - elevated_bid, normal_ask - normal_bid)

    def test_shock_suppresses_all_opening_quotes(self):
        self.assertEqual(build_quotes(self.book, Inventory(), self.limits, self.cfg, volatility_bps=50), [])

    def test_invalid_or_non_finite_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            build_quotes(Book(100, 99, 1, 1), Inventory(), self.limits, self.cfg)
        with self.assertRaises(ValueError):
            build_quotes(self.book, Inventory(), self.limits, self.cfg, volatility_bps=float("nan"))


if __name__ == "__main__":
    unittest.main()
