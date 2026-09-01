import random
import unittest

from entropy_mm.engine import EngineConfig, RiskMode
from entropy_mm.quote_model import Book, QuoteConfig, RiskLimits
from entropy_mm.simulation import SimState, SimVenue, apply_fill, run_cycle


CFG = QuoteConfig(tick_size=0.1, lot_size=0.01, layers=2, order_size=0.02)
LIMITS = RiskLimits(max_long=0.04, max_short=0.04, max_net=0.04, max_gross=0.04)
ENGINE = EngineConfig(min_order_lifetime_ms=0, reprice_threshold_bps=0.5, hard_reprice_threshold_bps=5.0)


class SimulationIntegrationTests(unittest.TestCase):
    def test_fill_then_next_cycle_respects_inventory_capacity(self):
        state = SimState()
        venue = SimVenue(state)
        book = Book(99.9, 100.1, 10, 10)
        decision, result = run_cycle(state, venue, now_ms=1_000, book=book, quote_config=CFG, risk_limits=LIMITS, engine_config=ENGINE)
        self.assertEqual(result.status, "executed")
        buy_ids = [oid for oid, order in state.open_orders.items() if order.side == "buy"]
        self.assertTrue(buy_ids)
        apply_fill(state, buy_ids[0])
        self.assertGreater(state.inventory, 0)
        run_cycle(state, venue, now_ms=2_000, book=book, quote_config=CFG, risk_limits=LIMITS, engine_config=ENGINE)
        self.assertLessEqual(state.inventory, LIMITS.max_net + 1e-12)
        total_buy = sum(order.size for order in state.open_orders.values() if order.side == "buy")
        self.assertLessEqual(state.inventory + total_buy, LIMITS.max_long + 1e-12)

    def test_cancel_unknown_blocks_replacement(self):
        state = SimState()
        venue = SimVenue(state)
        book = Book(99.9, 100.1, 10, 10)
        run_cycle(state, venue, now_ms=1_000, book=book, quote_config=CFG, risk_limits=LIMITS, engine_config=ENGINE)
        before = set(state.open_orders)
        venue.fail_cancel = True
        moved = Book(98.9, 99.1, 10, 10)
        _, result = run_cycle(state, venue, now_ms=2_000, book=moved, quote_config=CFG, risk_limits=LIMITS, engine_config=ENGINE)
        self.assertIn(result.status, {"cancel_rejected", "cancel_unconfirmed"})
        self.assertEqual(set(state.open_orders), before)

    def test_shock_cancels_opening_orders_and_does_not_replace(self):
        state = SimState()
        venue = SimVenue(state)
        book = Book(99.9, 100.1, 10, 10)
        run_cycle(state, venue, now_ms=1_000, book=book, quote_config=CFG, risk_limits=LIMITS, engine_config=ENGINE)
        self.assertTrue(state.open_orders)
        decision, result = run_cycle(state, venue, now_ms=2_000, book=book, quote_config=CFG, risk_limits=LIMITS, engine_config=ENGINE, volatility_bps=100.0)
        self.assertEqual(decision.mode, RiskMode.SHOCK)
        self.assertEqual(result.status, "cancelled_only")
        self.assertFalse(state.open_orders)

    def test_random_multicycle_never_exceeds_max_net_or_opening_capacity(self):
        rng = random.Random(7331)
        state = SimState()
        venue = SimVenue(state)
        mid = 100.0
        for cycle in range(1500):
            mid *= 1.0 + rng.uniform(-0.0008, 0.0008)
            spread = rng.uniform(0.05, 0.30)
            book = Book(mid - spread / 2, mid + spread / 2, rng.uniform(1, 30), rng.uniform(1, 30))
            vol = rng.uniform(0, 60)
            _, result = run_cycle(state, venue, now_ms=1_000 + cycle * 1000, book=book, quote_config=CFG, risk_limits=LIMITS, engine_config=ENGINE, volatility_bps=vol)
            if state.open_orders and rng.random() < 0.35:
                oid = rng.choice(list(state.open_orders))
                order = state.open_orders[oid]
                capacity = LIMITS.max_net - abs(state.inventory)
                if order.size <= capacity + 1e-12:
                    apply_fill(state, oid, fraction=1.0 if rng.random() < 0.7 else 0.5)
            self.assertLessEqual(abs(state.inventory), LIMITS.max_net + 1e-9)
            buy_open = sum(o.size for o in state.open_orders.values() if o.side == "buy" and not o.reduce_only)
            sell_open = sum(o.size for o in state.open_orders.values() if o.side == "sell" and not o.reduce_only)
            self.assertLessEqual(state.inventory + buy_open, LIMITS.max_long + 1e-9)
            self.assertLessEqual(-state.inventory + sell_open, LIMITS.max_short + 1e-9)
            if result.status == "executed":
                self.assertEqual(result.placement_state, "full")


if __name__ == "__main__":
    unittest.main()
