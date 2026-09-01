import unittest

from entropy_mm.quote_model import Quote
from entropy_mm.reconcile import LiveOrder, reconcile_orders


class ReconcileTests(unittest.TestCase):
    def test_exact_orders_are_kept(self):
        desired = [Quote(0, "buy", 99.9, 0.2), Quote(0, "sell", 100.1, 0.2)]
        existing = [LiveOrder(10, "buy", 99.9, 0.2), LiveOrder(11, "sell", 100.1, 0.2)]
        plan = reconcile_orders(existing, desired)
        self.assertEqual(plan.cancels, ())
        self.assertEqual(plan.places, ())
        self.assertEqual(plan.kept_order_ids, (10, 11))

    def test_stale_order_is_cancelled_before_missing_quote_is_placed(self):
        desired = [Quote(0, "buy", 99.8, 0.2)]
        plan = reconcile_orders([LiveOrder(10, "buy", 99.9, 0.2)], desired)
        self.assertEqual([item.order_id for item in plan.cancels], [10])
        self.assertEqual([(item.side, item.price, item.size) for item in plan.places], [("buy", 99.8, 0.2)])

    def test_duplicate_is_cancelled(self):
        desired = [Quote(0, "buy", 99.9, 0.2)]
        plan = reconcile_orders([LiveOrder(10, "buy", 99.9, 0.2), LiveOrder(11, "buy", 99.9, 0.2)], desired)
        self.assertEqual(plan.kept_order_ids, (10,))
        self.assertEqual([item.order_id for item in plan.cancels], [11])

    def test_reduce_only_exit_is_preserved(self):
        order = LiveOrder(12, "sell", 101.0, 0.2, reduce_only=True)
        plan = reconcile_orders([order], [])
        self.assertEqual(plan.kept_order_ids, (12,))
        self.assertEqual(plan.cancels, ())

    def test_small_price_move_is_kept_inside_refresh_threshold(self):
        plan = reconcile_orders([LiveOrder(10, "buy", 99.99, 0.2)], [Quote(0, "buy", 100.0, 0.2)], reprice_threshold_bps=2.0)
        self.assertEqual(plan.kept_order_ids, (10,))
        self.assertEqual(plan.places, ())

    def test_young_order_is_kept_only_inside_hard_threshold(self):
        plan = reconcile_orders(
            [LiveOrder(10, "buy", 99.95, 0.2, created_at_ms=9_000)],
            [Quote(0, "buy", 100.0, 0.2)],
            now_ms=10_000,
            min_order_lifetime_ms=5_000,
            reprice_threshold_bps=2.0,
            hard_reprice_threshold_bps=8.0,
        )
        self.assertEqual(plan.kept_order_ids, (10,))
        self.assertEqual(plan.cancels, ())

    def test_materially_stale_young_order_is_cancelled_immediately(self):
        plan = reconcile_orders(
            [LiveOrder(10, "buy", 99.0, 0.2, created_at_ms=9_000)],
            [Quote(0, "buy", 100.0, 0.2)],
            now_ms=10_000,
            min_order_lifetime_ms=5_000,
            reprice_threshold_bps=2.0,
            hard_reprice_threshold_bps=8.0,
        )
        self.assertEqual(plan.cancels[0].reason, "hard_stale")
        self.assertEqual(plan.kept_order_ids, ())

    def test_old_order_outside_threshold_is_repriced(self):
        plan = reconcile_orders(
            [LiveOrder(10, "buy", 99.0, 0.2, created_at_ms=1_000)],
            [Quote(0, "buy", 100.0, 0.2)],
            now_ms=10_000,
            min_order_lifetime_ms=5_000,
            reprice_threshold_bps=2.0,
        )
        self.assertEqual([item.order_id for item in plan.cancels], [10])
        self.assertEqual([item.price for item in plan.places], [100.0])

    def test_new_places_carry_cycle_epoch(self):
        plan = reconcile_orders([], [Quote(0, "buy", 100.0, 0.2)], quote_epoch=12345)
        self.assertEqual(plan.places[0].quote_epoch, 12345)


if __name__ == "__main__":
    unittest.main()
