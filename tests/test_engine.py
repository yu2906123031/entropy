import unittest

from entropy_mm.engine import CycleInput, EngineConfig, RiskMode, plan_cycle
from entropy_mm.quote_model import Book, Inventory, QuoteConfig, RiskLimits
from entropy_mm.reconcile import LiveOrder


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.book = Book(99.9, 100.1, 5, 5)
        self.inventory = Inventory()
        self.quote_config = QuoteConfig(tick_size=0.1, lot_size=0.01, order_size=0.2)
        self.limits = RiskLimits(1.0, 1.0, 0.6, 1.2)
        self.config = EngineConfig()

    def cycle(self, **changes):
        values = {
            "now_ms": 10_000,
            "book_time_ms": 9_900,
            "book": self.book,
            "venue_inventory": self.inventory,
            "ledger_inventory": self.inventory,
        }
        values.update(changes)
        return CycleInput(**values)

    def test_normal_cycle_builds_and_places_two_sided_quotes(self):
        decision = plan_cycle(self.cycle(), self.quote_config, self.limits, self.config)
        self.assertEqual(decision.mode, RiskMode.NORMAL)
        self.assertEqual(len(decision.quotes), 6)
        self.assertEqual(len(decision.plan.places), 6)

    def test_elevated_cycle_reduces_quote_intensity(self):
        decision = plan_cycle(
            self.cycle(volatility_bps=25), self.quote_config, self.limits, self.config
        )
        self.assertEqual(decision.mode, RiskMode.ELEVATED)
        self.assertEqual(len(decision.quotes), 4)

    def test_shock_cancels_opening_and_preserves_reduce_only(self):
        orders = (
            LiveOrder(1, "buy", 99, 0.2),
            LiveOrder(2, "sell", 101, 0.2, reduce_only=True),
        )
        decision = plan_cycle(
            self.cycle(volatility_bps=50, open_orders=orders),
            self.quote_config,
            self.limits,
            self.config,
        )
        self.assertEqual(decision.mode, RiskMode.SHOCK)
        self.assertEqual([x.order_id for x in decision.plan.cancels], [1])
        self.assertEqual(decision.plan.kept_order_ids, (2,))

    def test_stale_book_halts(self):
        decision = plan_cycle(
            self.cycle(book_time_ms=6_000), self.quote_config, self.limits, self.config
        )
        self.assertEqual((decision.mode, decision.reason), (RiskMode.HALTED, "stale_book"))

    def test_position_mismatch_halts(self):
        decision = plan_cycle(
            self.cycle(venue_inventory=Inventory(long=0.2)),
            self.quote_config,
            self.limits,
            self.config,
        )
        self.assertEqual(decision.reason, "position_mismatch")

    def test_margin_and_daily_loss_limits_halt(self):
        margin = plan_cycle(
            self.cycle(margin_usage_ratio=0.5), self.quote_config, self.limits, self.config
        )
        loss = plan_cycle(
            self.cycle(daily_pnl=-10), self.quote_config, self.limits, self.config
        )
        self.assertEqual(margin.reason, "margin_limit")
        self.assertEqual(loss.reason, "daily_loss_limit")


if __name__ == "__main__":
    unittest.main()
