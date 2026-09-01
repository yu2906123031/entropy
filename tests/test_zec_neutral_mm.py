from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from scripts import zec_neutral_mm as mm


class PlaceTests(unittest.TestCase):
    def test_post_only_cross_rejection_is_recoverable(self) -> None:
        exchange = Mock()
        exchange.order.return_value = {
            "response": {
                "data": {
                    "statuses": [
                        {"error": "Post only order would have immediately matched, bbo was 100@101. asset=214"}
                    ]
                }
            }
        }

        accepted = mm.place(exchange, True, 0.03, 101.0, False)

        self.assertFalse(accepted)

    def test_unexpected_order_rejection_remains_fatal(self) -> None:
        exchange = Mock()
        exchange.order.return_value = {
            "response": {"data": {"statuses": [{"error": "insufficient margin"}]}}
        }

        with self.assertRaisesRegex(RuntimeError, "insufficient margin"):
            mm.place(exchange, True, 0.03, 100.0, False)


class QuoteTests(unittest.TestCase):
    def test_aged_inventory_joins_touch_for_passive_exit(self) -> None:
        self.assertEqual(
            mm.inventory_exit_strategy_price(
                entry_price=100.0,
                szi=0.02,
                best_bid=99.8,
                best_ask=99.9,
                age_seconds=mm.SOFT_EXIT_AGE_SECONDS,
            ),
            99.9,
        )

    def test_fresh_inventory_keeps_profitable_exit_target(self) -> None:
        self.assertGreater(
            mm.inventory_exit_strategy_price(
                entry_price=100.0,
                szi=0.02,
                best_bid=99.8,
                best_ask=99.9,
                age_seconds=mm.SOFT_EXIT_AGE_SECONDS - 1,
            ),
            100.0,
        )

    def test_microprice_moves_toward_larger_bid_depth(self) -> None:
        self.assertAlmostEqual(mm.microprice(100.0, 101.0, 9.0, 1.0), 100.9)

    def test_inventory_skew_moves_quote_center_toward_flattening(self) -> None:
        self.assertLess(mm.reservation_price(100.0, 0.02, 0.02), 100.0)
        self.assertGreater(mm.reservation_price(100.0, -0.02, 0.02), 100.0)

    def test_lighter_quote_model_covers_bbo_costs_and_side_buffers(self) -> None:
        bid, ask = mm.lighter_style_quotes(
            best_bid=99.9,
            best_ask=100.1,
            bid_size=9.0,
            ask_size=1.0,
            inventory=0.0,
            max_inventory=0.02,
            volatility_bps=2.0,
            bid_adverse_buffer_bps=0.0,
            ask_adverse_buffer_bps=8.0,
        )
        self.assertLessEqual(bid, 99.9)
        self.assertGreater(ask, 100.1)
        self.assertGreater(ask - 100.0, 100.0 - bid)

    def test_opening_orders_match_a_single_unpaused_side(self) -> None:
        orders = [{"side": "B", "sz": "0.02", "limitPx": "99.9", "reduceOnly": False}]
        desired = [(True, 0.02, 99.9)]
        self.assertTrue(mm.opening_orders_match(orders, desired))

    def test_matching_quote_pair_is_retained_inside_requote_band(self) -> None:
        orders = [
            {"side": "B", "sz": "0.02", "limitPx": "99.95", "reduceOnly": False},
            {"side": "A", "sz": "0.02", "limitPx": "101.05", "reduceOnly": False},
        ]
        self.assertTrue(mm.quote_pair_matches(orders, 0.02, 100.0, 101.0))

    def test_quote_pair_is_replaced_after_material_price_move(self) -> None:
        orders = [
            {"side": "B", "sz": "0.02", "limitPx": "99.0", "reduceOnly": False},
            {"side": "A", "sz": "0.02", "limitPx": "102.0", "reduceOnly": False},
        ]
        self.assertFalse(mm.quote_pair_matches(orders, 0.02, 100.0, 101.0))

    def test_requote_band_is_five_bps(self) -> None:
        order = {"side": "B", "sz": "0.02", "limitPx": "99.96", "reduceOnly": False}
        self.assertTrue(mm.order_matches(order, True, 0.02, 100.0, False))

    def test_opening_quotes_receive_short_queue_lifetime(self) -> None:
        orders = [
            {"timestamp": 993_001},
            {"timestamp": 992_000},
        ]
        self.assertFalse(mm.minimum_quote_lifetime_elapsed(orders, now_ms=1_000_000))
        self.assertTrue(mm.minimum_quote_lifetime_elapsed(orders, now_ms=1_001_001))

    def test_missing_order_timestamp_allows_safe_cleanup(self) -> None:
        self.assertTrue(mm.minimum_quote_lifetime_elapsed([{"oid": 1}], now_ms=1_000_000))

    def test_exit_order_requires_reduce_only(self) -> None:
        opening_order = [{"side": "A", "sz": "0.02", "limitPx": "101.0", "reduceOnly": False}]
        exit_order = [{"side": "A", "sz": "0.02", "limitPx": "101.0", "reduceOnly": True}]
        self.assertFalse(mm.exit_order_matches(opening_order, False, 0.02, 101.0))
        self.assertTrue(mm.exit_order_matches(exit_order, False, 0.02, 101.0))

    def test_quotes_are_kept_on_the_passive_side_of_bbo(self) -> None:
        info = Mock()
        info.l2_snapshot.return_value = {
            "levels": [[{"px": "100.04"}], [{"px": "100.06"}]]
        }

        bid, ask = mm.passive_quotes(info, strategy_bid=100.2, strategy_ask=99.9)

        self.assertEqual(bid, 100.0)
        self.assertEqual(ask, 100.1)

    def test_quotes_preserve_more_conservative_strategy_prices(self) -> None:
        info = Mock()
        info.l2_snapshot.return_value = {
            "levels": [[{"px": "100.0"}], [{"px": "100.1"}]]
        }

        bid, ask = mm.passive_quotes(info, strategy_bid=99.8, strategy_ask=100.3)

        self.assertEqual(bid, 99.8)
        self.assertEqual(ask, 100.3)

    def test_long_inventory_exit_covers_fees_and_minimum_profit(self) -> None:
        bid, ask = mm.inventory_exit_quotes(
            entry_price=100.0,
            szi=0.03,
            round_trip_fee_bps=3.0,
            minimum_profit_bps=5.0,
        )

        self.assertIsNone(bid)
        self.assertGreaterEqual(ask, 100.08)

    def test_short_inventory_exit_covers_fees_and_minimum_profit(self) -> None:
        bid, ask = mm.inventory_exit_quotes(
            entry_price=100.0,
            szi=-0.03,
            round_trip_fee_bps=3.0,
            minimum_profit_bps=5.0,
        )

        self.assertLessEqual(bid, 99.92)
        self.assertIsNone(ask)


class MarketRegimeTests(unittest.TestCase):
    def test_rms_volatility_preserves_jump_energy(self) -> None:
        self.assertAlmostEqual(mm.rms_returns_bps([100.0, 100.1, 100.0]), 10.0, places=1)

    def test_book_toxicity_widens_and_pauses_only_adverse_side(self) -> None:
        signal = mm.book_toxicity_signal(900.0, 100.0)
        self.assertGreater(signal[2], 0.0)
        self.assertFalse(signal[3])
        self.assertTrue(signal[4])

    def test_adaptive_spread_covers_cost_floor(self) -> None:
        self.assertEqual(
            mm.adaptive_spread_bps([100.0, 100.0, 100.0], base_spread_bps=2.0),
            11.0,
        )

    def test_adaptive_spread_expands_with_realized_volatility(self) -> None:
        spread = mm.adaptive_spread_bps(
            [100.0, 100.4, 99.8, 100.5],
            base_spread_bps=25.0,
        )
        self.assertGreater(spread, 25.0)

    def test_fast_move_halts_new_openings(self) -> None:
        self.assertTrue(mm.fast_market_active([100.0, 100.1, 100.4], threshold_bps=30.0))
        self.assertFalse(mm.fast_market_active([100.0, 100.1, 100.2], threshold_bps=30.0))

    def test_choppy_volatility_remains_quoteable(self) -> None:
        mids = [100.0, 100.5, 99.8, 100.6, 99.9, 100.4]
        self.assertLess(mm.directional_efficiency(mids), 0.20)
        self.assertFalse(mm.fast_market_active(mids, threshold_bps=30.0))

    def test_directional_efficiency_identifies_one_way_move(self) -> None:
        self.assertGreater(mm.directional_efficiency([100.0, 100.1, 100.2, 100.4]), 0.95)

    def test_medium_trend_requires_longer_sample_and_halts_directional_market(self) -> None:
        mids = [100.0] * 19 + [100.5]
        self.assertTrue(mm.medium_trend_active(mids, threshold_bps=45.0))
        self.assertFalse(mm.medium_trend_active(mids[:19], threshold_bps=45.0))

    def test_toxic_order_book_halts_new_openings(self) -> None:
        self.assertTrue(mm.toxic_order_book(900.0, 100.0))
        self.assertTrue(mm.toxic_order_book(100.0, 900.0))
        self.assertFalse(mm.toxic_order_book(500.0, 500.0))

    def test_book_toxicity_uses_five_level_depth(self) -> None:
        info = Mock()
        info.l2_snapshot.return_value = {
            "levels": [
                [{"sz": "1"}, {"sz": "2"}, {"sz": "3"}, {"sz": "4"}, {"sz": "5"}, {"sz": "100"}],
                [{"sz": "5"}, {"sz": "4"}, {"sz": "3"}, {"sz": "2"}, {"sz": "1"}, {"sz": "100"}],
            ]
        }
        self.assertEqual(mm.top_book_sizes(info), (15.0, 15.0))

    def test_inventory_exit_triggers_on_age_or_adverse_pnl(self) -> None:
        self.assertTrue(mm.inventory_hard_exit_required(-0.6, 10, max_loss_usd=0.5, max_age_seconds=900))
        self.assertTrue(mm.inventory_hard_exit_required(0.1, 901, max_loss_usd=0.5, max_age_seconds=900))
        self.assertTrue(mm.inventory_hard_exit_required(-0.01, 10, adverse_move_bps=16.0))
        self.assertFalse(mm.inventory_hard_exit_required(-0.2, 300, max_loss_usd=0.5, max_age_seconds=900, max_adverse_move_bps=50.0))

    def test_inventory_adverse_move_is_side_aware(self) -> None:
        self.assertAlmostEqual(mm.inventory_adverse_move_bps(100.0, 99.8, 0.02), 20.0)
        self.assertAlmostEqual(mm.inventory_adverse_move_bps(100.0, 100.2, -0.02), 20.0)
        self.assertEqual(mm.inventory_adverse_move_bps(100.0, 100.2, 0.02), 0.0)


class RiskTests(unittest.TestCase):
    def test_session_net_pnl_includes_realized_losses_fees_and_upnl(self) -> None:
        fills = [
            {"closedPnl": "-3.0", "fee": "1.0"},
            {"closedPnl": "0.5", "fee": "0.2"},
        ]

        self.assertAlmostEqual(mm.session_net_pnl(fills, unrealized_pnl=-1.5), -5.2)

    def test_daily_loss_limit_blocks_new_opening_orders(self) -> None:
        self.assertTrue(mm.loss_limit_reached(-1.01, limit_usd=1.0))
        self.assertFalse(mm.loss_limit_reached(-0.99, limit_usd=1.0))

    def test_persistent_risk_halt_only_covers_session_level_limits(self) -> None:
        self.assertFalse(mm.persistent_risk_halt_required(0.0, -0.01, False, max_net_size=0.0201, max_loss_usd=0.30))
        self.assertTrue(mm.persistent_risk_halt_required(0.03, 0.0, False, max_net_size=0.0201, max_loss_usd=0.30))
        self.assertTrue(mm.persistent_risk_halt_required(0.0, -0.31, False, max_net_size=0.0201, max_loss_usd=0.30))
        self.assertTrue(mm.persistent_risk_halt_required(0.0, 0.01, True, max_net_size=0.0201, max_loss_usd=0.30))

    def test_daily_profit_lock_preserves_positive_session(self) -> None:
        self.assertTrue(mm.profit_lock_reached(0.011, target_usd=0.01))
        self.assertFalse(mm.profit_lock_reached(0.009, target_usd=0.01))

    def test_opening_gate_is_locked_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(mm.opening_gate_enabled())

    def test_opening_gate_requires_exact_explicit_enable(self) -> None:
        with patch.dict("os.environ", {"ENTROPY_ALLOW_NEW_OPENINGS": "true"}):
            self.assertTrue(mm.opening_gate_enabled())

    def test_recent_fill_activates_extended_cooldown(self) -> None:
        fills = [{"time": 880_001}]
        self.assertTrue(mm.post_fill_cooldown_active(fills, now_ms=1_000_000))

    def test_fill_older_than_extended_cooldown_clears(self) -> None:
        fills = [{"time": 879_999}]
        self.assertFalse(mm.post_fill_cooldown_active(fills, now_ms=1_000_000))

    def test_live_risk_constants_bound_tail_loss(self) -> None:
        self.assertLessEqual(mm.REFRESH_SECONDS, 2)
        self.assertLessEqual(mm.MAX_INVENTORY_AGE_SECONDS, 35)
        self.assertLessEqual(mm.MAX_INVENTORY_LOSS_USD, 0.04)
        self.assertLessEqual(mm.MAX_ADVERSE_MOVE_BPS, 10.0)
        self.assertEqual(mm.MAX_LOSS_USD, 1.0)


if __name__ == "__main__":
    unittest.main()
