import unittest

from entropy_mm.attribution import aggregate_expectancy, maker_expectancy


class AttributionTests(unittest.TestCase):
    def test_buy_spread_capture_and_costs(self):
        item = maker_expectancy(side="buy", fill_price=99.9, fair_value=100.0, markout_bps=-3.0, fee_bps=2.0, funding_bps=1.0)
        self.assertGreater(item.spread_capture_bps, 9.0)
        self.assertLess(item.net_bps, item.spread_capture_bps)

    def test_sell_spread_capture(self):
        item = maker_expectancy(side="sell", fill_price=100.1, fair_value=100.0)
        self.assertGreater(item.spread_capture_bps, 9.0)

    def test_aggregate_is_mean(self):
        a = maker_expectancy(side="buy", fill_price=99.9, fair_value=100.0)
        b = maker_expectancy(side="buy", fill_price=99.8, fair_value=100.0)
        agg = aggregate_expectancy([a, b])
        self.assertGreater(agg.net_bps, a.net_bps)
        self.assertLess(agg.net_bps, b.net_bps)


if __name__ == "__main__":
    unittest.main()
