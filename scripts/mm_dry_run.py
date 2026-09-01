#!/usr/bin/env python3
"""Read-only adaptive market-making plan and adaptive-v4 preview for ZEC."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperliquid.info import Info
from hyperliquid.utils import constants

from entropy_mm.adaptive_strategy import adaptive_quote_pair
from entropy_mm.closed_loop import capture_orders, check_position, finalize_cycle, managed_ids_after_execution
from entropy_mm.edge import profitability_edge
from entropy_mm.engine import CycleInput, EngineConfig, plan_cycle
from entropy_mm.execution import ExecutionMode, execute_plan
from entropy_mm.fill_sync import sync_fills
from entropy_mm.ledger import LotLedger
from entropy_mm.microstructure import depth_signal
from entropy_mm.operations import OperationsStore, assess_window
from entropy_mm.quote_model import Book, Inventory, QuoteConfig, RiskLimits, fair_value
from entropy_mm.quote_quality import QuoteQualityStore, empirical_fill_multiplier
from entropy_mm.reconcile import LiveOrder
from entropy_mm.toxicity import MarkoutStore

ACCOUNT = os.environ.get("ENTROPY_ACCOUNT", "0x78605485604BA45ce0eF860DB1594ec810154477")
COIN = os.environ.get("ENTROPY_COIN", "ZEC")
MAX_BOOK_AGE_MS = int(os.environ.get("ENTROPY_MAX_BOOK_AGE_MS", "3000"))
LEDGER_PATH = os.environ.get("ENTROPY_LEDGER_PATH", "runtime/entropy_zec_mm.sqlite3")
FILL_LOOKBACK_DAYS = int(os.environ.get("ENTROPY_FILL_LOOKBACK_DAYS", "30"))
OPERATIONS_PATH = os.environ.get("ENTROPY_OPERATIONS_PATH", "runtime/entropy_mm_operations.sqlite3")
MARKOUT_PATH = os.environ.get("ENTROPY_MARKOUT_PATH", "runtime/entropy_markouts.sqlite3")
QUALITY_PATH = os.environ.get("ENTROPY_QUOTE_QUALITY_PATH", "runtime/entropy_quote_quality.sqlite3")
OBSERVATION_WINDOW = max(1, int(os.getenv("ENTROPY_OBSERVATION_WINDOW", "100")))


class NoCallVenue:
    def cancel_orders(self, order_ids):
        raise AssertionError("dry-run attempted cancellation")

    def place_orders(self, places):
        raise AssertionError("dry-run attempted placement")

    def open_order_ids(self):
        raise AssertionError("dry-run attempted order query through execution venue")


def position_inventory(state: dict) -> Inventory:
    for item in state.get("assetPositions", []):
        position = item.get("position", {})
        if position.get("coin") == COIN:
            signed_size = float(position.get("szi", 0) or 0)
            return Inventory(long=max(signed_size, 0.0), short=max(-signed_size, 0.0))
    return Inventory()


def parse_orders(rows: list[dict]) -> list[LiveOrder]:
    orders = []
    for row in rows:
        if row.get("coin") != COIN:
            continue
        orders.append(
            LiveOrder(
                order_id=int(row["oid"]),
                side="buy" if row["side"] == "B" else "sell",
                price=float(row["limitPx"]),
                size=float(row["sz"]),
                reduce_only=bool(row.get("reduceOnly", False)),
                created_at_ms=int(row["timestamp"]) if row.get("timestamp") else None,
            )
        )
    return orders


def quote_config() -> QuoteConfig:
    return QuoteConfig(
        tick_size=float(os.getenv("ENTROPY_TICK_SIZE", "0.001")),
        lot_size=float(os.getenv("ENTROPY_LOT_SIZE", "0.01")),
        layers=int(os.getenv("ENTROPY_LAYERS", "3")),
        base_half_spread_bps=float(os.getenv("ENTROPY_BASE_HALF_SPREAD_BPS", "35")),
        level_gap_bps=float(os.getenv("ENTROPY_LEVEL_GAP_BPS", "6")),
        inventory_skew_bps=float(os.getenv("ENTROPY_INVENTORY_SKEW_BPS", "20")),
        order_size=float(os.getenv("ENTROPY_ORDER_SIZE", "0.02")),
        elevated_vol_bps=float(os.getenv("ENTROPY_ELEVATED_VOL_BPS", "20")),
        shock_vol_bps=float(os.getenv("ENTROPY_SHOCK_VOL_BPS", "50")),
        elevated_spread_multiplier=float(os.getenv("ENTROPY_ELEVATED_SPREAD_MULTIPLIER", "1.8")),
        elevated_size_multiplier=float(os.getenv("ENTROPY_ELEVATED_SIZE_MULTIPLIER", "0.5")),
        microprice_weight=float(os.getenv("ENTROPY_MICROPRICE_WEIGHT", "0.65")),
        max_microprice_edge_bps=float(os.getenv("ENTROPY_MAX_MICROPRICE_EDGE_BPS", "4")),
        inventory_skew_power=float(os.getenv("ENTROPY_INVENTORY_SKEW_POWER", "1.5")),
        capacity_skew_strength=float(os.getenv("ENTROPY_CAPACITY_SKEW_STRENGTH", "0.85")),
    )


def risk_limits() -> RiskLimits:
    max_net = float(os.getenv("ENTROPY_MAX_NET_SIZE", "0.0201"))
    return RiskLimits(
        max_long=float(os.getenv("ENTROPY_MAX_LONG", str(max_net))),
        max_short=float(os.getenv("ENTROPY_MAX_SHORT", str(max_net))),
        max_net=max_net,
        max_gross=float(os.getenv("ENTROPY_MAX_GROSS", str(max_net * 2))),
    )


def adaptive_v4_preview(snapshot: dict, inventory: Inventory, config: QuoteConfig, limits: RiskLimits, volatility_bps: float) -> dict:
    signal = depth_signal(snapshot["levels"], depth_levels=5)
    markouts = MarkoutStore(MARKOUT_PATH)
    aggregate = markouts.summary(5000, last_n=100, toxic_mean_bps=-2.0, min_samples=10)
    buy = markouts.summary(5000, side="buy", last_n=100, toxic_mean_bps=-2.0, min_samples=10)
    sell = markouts.summary(5000, side="sell", last_n=100, toxic_mean_bps=-2.0, min_samples=10)
    quality_store = QuoteQualityStore(QUALITY_PATH)
    quality = quality_store.quality(last_n=200)
    buy_quality = quality_store.quality(side="buy", last_n=200)
    sell_quality = quality_store.quality(side="sell", last_n=200)
    fill_multiplier = min(
        empirical_fill_multiplier(quality),
        max(0.35, (empirical_fill_multiplier(buy_quality) + empirical_fill_multiplier(sell_quality)) / 2.0),
    )
    direction = float(os.getenv("ENTROPY_DIRECTIONAL_BPS", "0"))
    edge = profitability_edge(
        volatility_bps=volatility_bps,
        book_imbalance=signal.imbalance,
        markout_mean_bps=aggregate.mean_markout_bps,
        markout_negative_rate=aggregate.negative_rate,
        buy_markout_mean_bps=buy.mean_markout_bps,
        buy_markout_negative_rate=buy.negative_rate,
        sell_markout_mean_bps=sell.mean_markout_bps,
        sell_markout_negative_rate=sell.negative_rate,
        directional_bps=direction,
        round_trip_fee_bps=float(os.getenv("ENTROPY_ROUND_TRIP_MAKER_FEE_BPS", "3")),
        minimum_profit_bps=float(os.getenv("ENTROPY_MINIMUM_NET_PROFIT_BPS", "8")),
        fill_size_multiplier=fill_multiplier,
    )
    best_bid = float(snapshot["levels"][0][0]["px"])
    best_ask = float(snapshot["levels"][1][0]["px"])
    pair = adaptive_quote_pair(
        best_bid=best_bid,
        best_ask=best_ask,
        fair_value=signal.fair_value,
        inventory=inventory.net,
        max_inventory=limits.max_net,
        volatility_bps=volatility_bps,
        bid_buffer_bps=0.0,
        ask_buffer_bps=0.0,
        edge=edge,
        base_half_spread_bps=config.base_half_spread_bps,
        round_trip_fee_bps=float(os.getenv("ENTROPY_ROUND_TRIP_MAKER_FEE_BPS", "3")),
        minimum_profit_bps=float(os.getenv("ENTROPY_MINIMUM_NET_PROFIT_BPS", "8")),
        base_size=config.order_size,
        lot_size=config.lot_size,
        inventory_skew_bps=config.inventory_skew_bps,
        inventory_skew_power=config.inventory_skew_power,
    )
    return {
        "l2": asdict(signal),
        "edge": asdict(edge),
        "quote_pair": asdict(pair),
        "pause_bid": edge.pause_bid or buy.toxic,
        "pause_ask": edge.pause_ask or sell.toxic,
        "markout": {"aggregate": asdict(aggregate), "buy": asdict(buy), "sell": asdict(sell)},
        "fill_quality": {"aggregate": asdict(quality), "buy": asdict(buy_quality), "sell": asdict(sell_quality), "learned_multiplier": fill_multiplier},
        "directional_bps": direction,
    }


def main() -> None:
    cycle_started = time.perf_counter()
    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    ledger = LotLedger(LEDGER_PATH)
    sync_end_ms = int(time.time() * 1000)
    sync_result = sync_fills(info, ledger, address=ACCOUNT, coin=COIN, initial_start_ms=sync_end_ms - FILL_LOOKBACK_DAYS * 86_400_000, end_ms=sync_end_ms)
    ledger_snapshot = ledger.snapshot()
    snapshot = info.l2_snapshot(COIN)
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - int(snapshot["time"])
    if age_ms < 0 or age_ms > MAX_BOOK_AGE_MS:
        raise SystemExit(f"stale order book: age_ms={age_ms}")
    bids, asks = snapshot["levels"]
    if not bids or not asks:
        raise SystemExit("empty order book side")
    book = Book(float(bids[0]["px"]), float(asks[0]["px"]), float(bids[0]["sz"]), float(asks[0]["sz"]))
    state = info.user_state(ACCOUNT)
    inventory = position_inventory(state)
    config = quote_config()
    limits = risk_limits()
    volatility_bps = float(os.getenv("ENTROPY_VOLATILITY_BPS", "0"))
    margin = state.get("marginSummary", {})
    account_value = float(margin.get("accountValue", 0) or 0)
    margin_used = float(margin.get("totalMarginUsed", 0) or 0)
    margin_usage_ratio = margin_used / account_value if account_value > 0 else 0.0
    engine_config = EngineConfig(
        max_book_age_ms=MAX_BOOK_AGE_MS,
        position_mismatch_tolerance=float(os.getenv("ENTROPY_POSITION_MISMATCH_TOLERANCE", "0.001")),
        max_margin_usage_ratio=float(os.getenv("ENTROPY_MAX_MARGIN_USAGE_RATIO", "0.50")),
        daily_loss_limit=float(os.getenv("ENTROPY_MAX_LOSS_USD", "1.0")),
        min_order_lifetime_ms=int(os.getenv("ENTROPY_MIN_ORDER_LIFETIME_MS", "5000")),
        reprice_threshold_bps=float(os.getenv("ENTROPY_REPRICE_THRESHOLD_BPS", "2")),
        hard_reprice_threshold_bps=float(os.getenv("ENTROPY_HARD_REPRICE_THRESHOLD_BPS", "8")),
    )
    decision = plan_cycle(
        CycleInput(
            now_ms=now_ms,
            book_time_ms=int(snapshot["time"]),
            book=book,
            venue_inventory=inventory,
            ledger_inventory=ledger_snapshot.inventory,
            open_orders=tuple(parse_orders(info.open_orders(ACCOUNT))),
            volatility_bps=volatility_bps,
            margin_usage_ratio=margin_usage_ratio,
            daily_pnl=float(os.getenv("ENTROPY_DAILY_PNL", "0")),
        ),
        config,
        limits,
        engine_config,
    )
    plan = decision.plan
    execution = execute_plan(plan, NoCallVenue(), mode=ExecutionMode.DRY_RUN)
    operations = OperationsStore(OPERATIONS_PATH)

    post_sync_end_ms = int(time.time() * 1000)
    post_sync = sync_fills(info, ledger, address=ACCOUNT, coin=COIN, initial_start_ms=post_sync_end_ms - FILL_LOOKBACK_DAYS * 86_400_000, end_ms=post_sync_end_ms)
    post_ledger = ledger.snapshot()
    post_inventory = position_inventory(info.user_state(ACCOUNT))
    post_rows = info.open_orders(ACCOUNT)
    order_snapshot = capture_orders(post_rows, managed_ids_after_execution(operations, execution), coin=COIN)
    position = check_position(post_inventory, post_ledger.inventory)
    observed_quotes = len(plan.kept_order_ids) + len(plan.cancels)
    closure = finalize_cycle(
        operations,
        execution,
        order_snapshot,
        position,
        planned_cancels=len(plan.cancels),
        planned_places=len(plan.places),
        synced_fills=post_sync.applied,
        cycle_succeeded=position.matched,
        latency_ms=(time.perf_counter() - cycle_started) * 1000,
        kept_quotes=len(plan.kept_order_ids),
        observed_quotes=observed_quotes,
        window_size=OBSERVATION_WINDOW,
    )
    output = {
        "mode": "read_only_dry_run",
        "account": ACCOUNT,
        "coin": COIN,
        "book_age_ms": age_ms,
        "book": asdict(book),
        "fair_value": fair_value(book, config),
        "adaptive_v4_preview": adaptive_v4_preview(snapshot, inventory, config, limits, volatility_bps),
        "inventory": asdict(inventory),
        "quote_config": asdict(config),
        "risk_limits": asdict(limits),
        "engine_config": asdict(engine_config),
        "ledger": {"path": LEDGER_PATH, "inventory": asdict(ledger_snapshot.inventory), "realized_pnl": ledger_snapshot.realized_pnl, "fees": ledger_snapshot.fees, "trade_count": ledger_snapshot.trade_count, "fill_sync": asdict(sync_result)},
        "risk": {"mode": decision.mode.value, "reason": decision.reason},
        "margin_usage_ratio": margin_usage_ratio,
        "quotes": [asdict(item) for item in decision.quotes],
        "reconciliation": {"cancel_first": [asdict(item) for item in plan.cancels], "then_place": [asdict(item) for item in plan.places], "kept_order_ids": list(plan.kept_order_ids)},
        "post_execution": {"execution": asdict(execution), "forced_fill_sync": asdict(post_sync), "order_snapshot": asdict(order_snapshot), "position": asdict(position), "metrics": asdict(closure.metrics), "window_metrics": asdict(closure.window_metrics), "window_assessment": asdict(assess_window(closure.window_metrics)), "operations_path": OPERATIONS_PATH},
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
