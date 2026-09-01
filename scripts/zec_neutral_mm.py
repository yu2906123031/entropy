#!/usr/bin/env python3
"""Adaptive bounded ZEC market maker with learned microstructure profitability gates."""
from __future__ import annotations

import getpass
import json
import math
import os
import signal
import statistics
import time
from collections import deque
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from entropy_mm.edge import directional_pressure_bps, dynamic_order_size, profitability_edge
from entropy_mm.microstructure import depth_signal, distance_from_touch_bps, queue_ahead_size
from entropy_mm.quote_quality import Exposure, QuoteQualityStore, empirical_fill_multiplier
from entropy_mm.toxicity import FillObservation, MarkoutStore, MarkoutTracker

ACCOUNT = os.getenv("ENTROPY_ACCOUNT", "0x78605485604BA45ce0eF860DB1594ec810154477")
AGENT = os.getenv("ENTROPY_AGENT", "0x3B4347B99BB749eBdD6DE720736796E1b7Dfe4a6")
COIN = os.getenv("ENTROPY_COIN", "ZEC")
BASE_ORDER_SIZE = float(os.getenv("ENTROPY_ORDER_SIZE", "0.02"))
LEVEL_SIZES = (BASE_ORDER_SIZE,)
LEVEL_SPREAD_BPS = (float(os.getenv("ENTROPY_BASE_HALF_SPREAD_BPS", "35")),)
CAPITAL_BUDGET_USD = float(os.getenv("ENTROPY_CAPITAL_BUDGET_USD", "20"))
MAX_NET_SIZE = float(os.getenv("ENTROPY_MAX_NET_SIZE", "0.0201"))
MAX_LOSS_USD = float(os.getenv("ENTROPY_MAX_LOSS_USD", "1.0"))
REFRESH_SECONDS = 2
REQUOTE_THRESHOLD_BPS = 5.0
HARD_REQUOTE_THRESHOLD_BPS = 12.0
SAFE_CYCLES_TO_QUOTE = 5
TOXIC_CYCLES_TO_HALT = 2
MINIMUM_QUOTE_LIFETIME_SECONDS = 8
POST_FILL_COOLDOWN_SECONDS = 120
ROUND_TRIP_MAKER_FEE_BPS = 3.0
MINIMUM_NET_PROFIT_BPS = 8.0
FAST_MARKET_THRESHOLD_BPS = 25.0
MEDIUM_TREND_THRESHOLD_BPS = 45.0
TOXIC_BOOK_IMBALANCE = 0.80
VOLATILITY_SPREAD_MULTIPLIER = 2.5
TOXIC_WIDEN_THRESHOLD = 0.45
TOXIC_PAUSE_THRESHOLD = 0.80
MAX_TOXIC_BUFFER_BPS = 15.0
INVENTORY_SKEW_BPS_AT_MAX = 20.0
INVENTORY_SKEW_POWER = 1.5
MICROPRICE_WEIGHT = 0.65
MAX_MICROPRICE_EDGE_BPS = 4.0
SOFT_EXIT_AGE_SECONDS = 15
MAX_INVENTORY_AGE_SECONDS = 35
MAX_INVENTORY_LOSS_USD = 0.035
MAX_ADVERSE_MOVE_BPS = 8.0
DAILY_PROFIT_LOCK_USD = 0.10
MARKET_CLOSE_SLIPPAGE = 0.005
MARKOUT_HORIZON_MS = 5_000
TOXIC_MARKOUT_MEAN_BPS = -2.0
TOXIC_MARKOUT_MIN_SAMPLES = 10
MARKOUT_PATH = os.getenv("ENTROPY_MARKOUT_PATH", "runtime/entropy_markouts.sqlite3")
QUALITY_PATH = os.getenv("ENTROPY_QUOTE_QUALITY_PATH", "runtime/entropy_quote_quality.sqlite3")
LOT_SIZE = float(os.getenv("ENTROPY_LOT_SIZE", "0.01"))
STRATEGY_RESET_ACK = "adaptive-v4"
KEYSTORE = Path(__file__).resolve().parents[1] / "secrets" / "agent-keystore.json"
BUILDER = {"b": "0xcd254d2a328f7f67c7c6fef930a4757516f7b601", "f": 0}
running = True


def stop(*_args) -> None:
    global running
    running = False


def read_password() -> str:
    path = os.environ.get("ENTROPY_CREDENTIAL_FILE")
    return Path(path).read_text().rstrip("\r\n") if path else getpass.getpass("Keystore password: ")


def zec_position(info: Info) -> dict | None:
    for item in info.user_state(ACCOUNT).get("assetPositions", []):
        position = item.get("position", {})
        if position.get("coin") == COIN and abs(float(position.get("szi", 0) or 0)) > 0:
            return position
    return None


def zec_open_orders(info: Info) -> list[dict]:
    return [order for order in info.frontend_open_orders(ACCOUNT) if order.get("coin") == COIN]


def cancel_orders(exchange: Exchange, orders: list[dict]) -> None:
    for order in orders:
        result = exchange.cancel(COIN, int(order["oid"]))
        print(json.dumps({"event": "cancel", "oid": order["oid"], "result": result}), flush=True)


def cancel_all(exchange: Exchange, info: Info) -> None:
    cancel_orders(exchange, zec_open_orders(info))


def order_price_distance_bps(order: dict, price: float) -> float:
    live = float(order.get("limitPx", 0) or 0)
    return math.inf if live <= 0 or price <= 0 else abs(live / price - 1.0) * 10_000


def order_matches(order: dict, buy: bool, size: float, price: float, reduce_only: bool, threshold_bps: float = REQUOTE_THRESHOLD_BPS) -> bool:
    return (
        order.get("side") == ("B" if buy else "A")
        and abs(float(order.get("sz", 0) or 0) - size) < 1e-9
        and bool(order.get("reduceOnly", False)) == reduce_only
        and order_price_distance_bps(order, price) < threshold_bps
    )


def quote_pair_matches(orders: list[dict], size: float, bid: float, ask: float) -> bool:
    return len(orders) == 2 and any(order_matches(o, True, size, bid, False) for o in orders) and any(order_matches(o, False, size, ask, False) for o in orders)


def opening_orders_match(orders: list[dict], desired: list[tuple[bool, float, float]]) -> bool:
    return len(orders) == len(desired) and all(any(order_matches(o, buy, size, price, False) for o in orders) for buy, size, price in desired)


def opening_orders_hard_stale(orders: list[dict], desired: list[tuple[bool, float, float]], threshold_bps: float = HARD_REQUOTE_THRESHOLD_BPS) -> bool:
    for order in orders:
        if order.get("reduceOnly"):
            continue
        side = order.get("side")
        candidates = [price for buy, size, price in desired if side == ("B" if buy else "A") and abs(float(order.get("sz", 0) or 0) - size) < 1e-9]
        if not candidates or min(order_price_distance_bps(order, price) for price in candidates) >= threshold_bps:
            return True
    return False


def minimum_quote_lifetime_elapsed(orders: list[dict], now_ms: int | None = None, minimum_seconds: float = MINIMUM_QUOTE_LIFETIME_SECONDS) -> bool:
    if not orders:
        return True
    timestamps = [int(order.get("timestamp", 0) or 0) for order in orders]
    if any(timestamp <= 0 for timestamp in timestamps):
        return True
    now = int(time.time() * 1000) if now_ms is None else now_ms
    return all(now - timestamp >= minimum_seconds * 1000 for timestamp in timestamps)


def exit_order_matches(orders: list[dict], buy: bool, size: float, price: float) -> bool:
    return len(orders) == 1 and order_matches(orders[0], buy, size, price, True)


def place(exchange: Exchange, buy: bool, size: float, price: float, reduce_only: bool) -> bool:
    result = exchange.order(COIN, buy, size, price, {"limit": {"tif": "Alo"}}, reduce_only, builder=BUILDER)
    status = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
    print(json.dumps({"event": "place", "side": "buy" if buy else "sell", "size": size, "price": price, "reduce_only": reduce_only, "status": status}), flush=True)
    if isinstance(status, dict) and "resting" in status:
        return True
    error = status.get("error", "") if isinstance(status, dict) else ""
    if "Post only order would have immediately matched" in error:
        return False
    raise RuntimeError(f"order rejected: {status}")


def passive_quotes(info: Info, strategy_bid: float, strategy_ask: float) -> tuple[float, float]:
    levels = info.l2_snapshot(COIN).get("levels", [])
    if len(levels) < 2 or not levels[0] or not levels[1]:
        raise RuntimeError("ZEC order book has no two-sided BBO")
    best_bid = Decimal(str(levels[0][0]["px"]))
    best_ask = Decimal(str(levels[1][0]["px"]))
    tick = Decimal(os.getenv("ENTROPY_PRICE_TICK", "0.1"))
    return (
        float(min(Decimal(str(strategy_bid)), best_bid).quantize(tick, rounding=ROUND_FLOOR)),
        float(max(Decimal(str(strategy_ask)), best_ask).quantize(tick, rounding=ROUND_CEILING)),
    )


def inventory_exit_quotes(entry_price: float, szi: float, round_trip_fee_bps: float = ROUND_TRIP_MAKER_FEE_BPS, minimum_profit_bps: float = MINIMUM_NET_PROFIT_BPS) -> tuple[float | None, float | None]:
    edge = (Decimal(str(round_trip_fee_bps)) + Decimal(str(minimum_profit_bps))) / Decimal("10000")
    entry = Decimal(str(entry_price))
    if szi > 0:
        return None, float(entry * (Decimal("1") + edge))
    if szi < 0:
        return float(entry * (Decimal("1") - edge)), None
    return None, None


def inventory_exit_strategy_price(entry_price: float, szi: float, best_bid: float, best_ask: float, age_seconds: float) -> float:
    bid, ask = inventory_exit_quotes(entry_price, szi)
    if age_seconds >= SOFT_EXIT_AGE_SECONDS:
        return best_ask if szi > 0 else best_bid
    target = ask if szi > 0 else bid
    if target is None:
        raise ValueError("inventory exit requires a non-zero position")
    return target


def adaptive_spread_bps(mids: list[float], base_spread_bps: float, round_trip_fee_bps: float = ROUND_TRIP_MAKER_FEE_BPS, minimum_profit_bps: float = MINIMUM_NET_PROFIT_BPS, volatility_multiplier: float = VOLATILITY_SPREAD_MULTIPLIER) -> float:
    floor = round_trip_fee_bps + minimum_profit_bps
    if len(mids) < 2:
        return max(base_spread_bps, floor)
    returns = [abs((current / previous - 1.0) * 10_000) for previous, current in zip(mids, mids[1:]) if previous > 0]
    return max(base_spread_bps, floor, statistics.fmean(returns) * volatility_multiplier if returns else 0.0)


def rms_returns_bps(mids: list[float]) -> float:
    returns = [(current / previous - 1.0) * 10_000 for previous, current in zip(mids, mids[1:]) if previous > 0]
    return (sum(value * value for value in returns) / len(returns)) ** 0.5 if returns else 0.0


def microprice(best_bid: float, best_ask: float, bid_size: float, ask_size: float) -> float:
    if best_bid <= 0 or best_ask <= best_bid:
        raise ValueError("invalid two-sided book")
    total = bid_size + ask_size
    return (best_bid + best_ask) / 2 if bid_size <= 0 or ask_size <= 0 or total <= 0 else (best_ask * bid_size + best_bid * ask_size) / total


def capped_fair_value(best_bid: float, best_ask: float, bid_size: float, ask_size: float) -> float:
    mid = (best_bid + best_ask) / 2
    edge = microprice(best_bid, best_ask, bid_size, ask_size) - mid
    cap = mid * MAX_MICROPRICE_EDGE_BPS / 10_000
    return mid + MICROPRICE_WEIGHT * max(-cap, min(cap, edge))


def reservation_price(fair: float, inventory: float, max_inventory: float, skew_bps_at_max: float = INVENTORY_SKEW_BPS_AT_MAX) -> float:
    ratio = max(-1.0, min(1.0, inventory / max_inventory)) if max_inventory > 0 else 0.0
    nonlinear = math.copysign(abs(ratio) ** INVENTORY_SKEW_POWER, ratio) if ratio else 0.0
    return fair * (1.0 - nonlinear * skew_bps_at_max / 10_000)


def book_toxicity_signal(bid_size: float, ask_size: float, widen_threshold: float = TOXIC_WIDEN_THRESHOLD, pause_threshold: float = TOXIC_PAUSE_THRESHOLD, max_buffer_bps: float = MAX_TOXIC_BUFFER_BPS) -> tuple[float, float, float, bool, bool]:
    total = bid_size + ask_size
    if bid_size <= 0 or ask_size <= 0 or total <= 0:
        return 0.0, 0.0, 0.0, False, False
    imbalance = max(-1.0, min(1.0, (bid_size - ask_size) / total))
    magnitude = abs(imbalance)
    span = max(1e-9, pause_threshold - widen_threshold)
    buffer = 0.0 if magnitude <= widen_threshold else max_buffer_bps * min(1.0, (magnitude - widen_threshold) / span)
    return (
        imbalance,
        buffer if imbalance < 0 else 0.0,
        buffer if imbalance > 0 else 0.0,
        imbalance < 0 and magnitude >= pause_threshold,
        imbalance > 0 and magnitude >= pause_threshold,
    )


def lighter_style_quotes(
    best_bid: float,
    best_ask: float,
    bid_size: float,
    ask_size: float,
    inventory: float,
    max_inventory: float,
    volatility_bps: float,
    bid_adverse_buffer_bps: float,
    ask_adverse_buffer_bps: float,
    base_half_spread_bps: float = LEVEL_SPREAD_BPS[0],
    required_edge_bps: float = 0.0,
    fair_value: float | None = None,
) -> tuple[float, float]:
    fair = capped_fair_value(best_bid, best_ask, bid_size, ask_size) if fair_value is None else fair_value
    center = reservation_price(fair, inventory, max_inventory)
    mid = (best_bid + best_ask) / 2
    observed = (best_ask - best_bid) / mid * 5_000
    vol_ratio = max(0.0, volatility_bps / max(FAST_MARKET_THRESHOLD_BPS, 1e-9))
    dynamic_vol = volatility_bps * (1.0 + min(2.0, vol_ratio * vol_ratio))
    half = max(base_half_spread_bps, observed + ROUND_TRIP_MAKER_FEE_BPS + MINIMUM_NET_PROFIT_BPS, required_edge_bps, dynamic_vol)
    return (
        min(center * (1.0 - (half + bid_adverse_buffer_bps) / 10_000), best_bid),
        max(center * (1.0 + (half + ask_adverse_buffer_bps) / 10_000), best_ask),
    )


def directional_efficiency(mids: list[float]) -> float:
    if len(mids) < 2 or any(value <= 0 for value in mids):
        return 0.0
    path = sum(abs((current / previous - 1.0) * 10_000) for previous, current in zip(mids, mids[1:]))
    return 0.0 if path <= 0 else min(1.0, abs((mids[-1] / mids[0] - 1.0) * 10_000) / path)


def fast_market_active(mids: list[float], threshold_bps: float = FAST_MARKET_THRESHOLD_BPS, trend_efficiency: float = 0.70) -> bool:
    return len(mids) >= 3 and mids[0] > 0 and abs((mids[-1] / mids[0] - 1.0) * 10_000) >= threshold_bps and directional_efficiency(mids) >= trend_efficiency


def medium_trend_active(mids: list[float], threshold_bps: float = MEDIUM_TREND_THRESHOLD_BPS, trend_efficiency: float = 0.55) -> bool:
    return len(mids) >= 20 and mids[0] > 0 and abs((mids[-1] / mids[0] - 1.0) * 10_000) >= threshold_bps and directional_efficiency(mids) >= trend_efficiency


def toxic_order_book(bid_size: float, ask_size: float, threshold: float = TOXIC_BOOK_IMBALANCE) -> bool:
    total = bid_size + ask_size
    if total <= 0:
        return True
    ratio = bid_size / total
    return ratio >= threshold or ratio <= 1.0 - threshold


def top_book_sizes(info: Info, depth_levels: int = 5) -> tuple[float, float]:
    levels = info.l2_snapshot(COIN).get("levels", [])
    if len(levels) < 2 or not levels[0] or not levels[1]:
        raise RuntimeError("ZEC order book has no two-sided liquidity")
    return (
        sum(float(level.get("sz", 0) or 0) for level in levels[0][:depth_levels]),
        sum(float(level.get("sz", 0) or 0) for level in levels[1][:depth_levels]),
    )


def inventory_hard_exit_required(unrealized_pnl: float, age_seconds: float, adverse_move_bps: float = 0.0, max_loss_usd: float = MAX_INVENTORY_LOSS_USD, max_age_seconds: float = MAX_INVENTORY_AGE_SECONDS, max_adverse_move_bps: float = MAX_ADVERSE_MOVE_BPS) -> bool:
    return unrealized_pnl <= -abs(max_loss_usd) or age_seconds >= max_age_seconds or adverse_move_bps >= max_adverse_move_bps


def inventory_adverse_move_bps(entry_price: float, mark_price: float, szi: float) -> float:
    if entry_price <= 0 or szi == 0:
        return 0.0
    signed_return = (mark_price / entry_price - 1.0) * 10_000
    return max(0.0, -signed_return if szi > 0 else signed_return)


def inventory_age_seconds(fills: list[dict], now_ms: int | None = None) -> float:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    latest = max((int(fill.get("time", 0) or 0) for fill in fills), default=now)
    return max(0.0, (now - latest) / 1000)


def opening_gate_enabled() -> bool:
    return os.getenv("ENTROPY_ALLOW_NEW_OPENINGS", "false").lower() == "true"


def strategy_reset_ack_enabled() -> bool:
    return os.getenv("ENTROPY_STRATEGY_RESET_ACK", "") == STRATEGY_RESET_ACK


def post_fill_cooldown_active(fills: list[dict], now_ms: int | None = None) -> bool:
    if not fills:
        return False
    now = int(time.time() * 1000) if now_ms is None else now_ms
    return now - max(int(fill.get("time", 0) or 0) for fill in fills) < POST_FILL_COOLDOWN_SECONDS * 1000


def session_net_pnl(fills: list[dict], unrealized_pnl: float = 0.0) -> float:
    return sum(float(fill.get("closedPnl", 0) or 0) - float(fill.get("fee", 0) or 0) for fill in fills) + unrealized_pnl


def loss_limit_reached(net_pnl: float, limit_usd: float = MAX_LOSS_USD) -> bool:
    return net_pnl <= -abs(limit_usd)


def persistent_risk_halt_required(szi: float, net_pnl: float, profit_locked: bool, max_net_size: float = MAX_NET_SIZE, max_loss_usd: float = MAX_LOSS_USD) -> bool:
    return abs(szi) > max_net_size or loss_limit_reached(net_pnl, max_loss_usd) or profit_locked


def profit_lock_reached(net_pnl: float, target_usd: float = DAILY_PROFIT_LOCK_USD) -> bool:
    return net_pnl >= abs(target_usd)


def trailing_24h_start_ms() -> int:
    return int(time.time() * 1000) - 86_400_000


def flatten(exchange: Exchange, info: Info) -> None:
    position = zec_position(info)
    if position:
        szi = float(position["szi"])
        result = exchange.market_close(COIN, abs(szi), None, MARKET_CLOSE_SLIPPAGE, builder=BUILDER)
        print(json.dumps({"event": "flatten", "szi": szi, "result": result}), flush=True)


def _fill_observation(fill: dict) -> FillObservation | None:
    try:
        raw = str(fill.get("side", ""))
        side = "buy" if raw in {"B", "buy", "Buy"} else "sell" if raw in {"A", "sell", "Sell"} else ""
        price = float(fill.get("px", fill.get("price", 0)) or 0)
        size = float(fill.get("sz", fill.get("size", 0)) or 0)
        timestamp = int(fill.get("time", 0) or 0)
        trade_id = str(fill.get("tid", fill.get("hash", f"{timestamp}:{side}:{price}:{size}")))
        return FillObservation(trade_id, side, price, size, timestamp) if side and price > 0 and size > 0 and timestamp > 0 else None
    except (TypeError, ValueError):
        return None


def _exposure_filled(exposure: Exposure, observations: list[FillObservation], tolerance_bps: float = 5.0) -> bool:
    return any(
        observation.side == exposure.side
        and observation.time_ms >= exposure.created_at_ms
        and abs(observation.price / exposure.price - 1.0) * 10_000 <= tolerance_bps
        for observation in observations
    )


def main() -> None:
    key = Account.decrypt(json.loads(KEYSTORE.read_text()), read_password())
    agent = Account.from_key(key)
    if agent.address.lower() != AGENT.lower():
        raise SystemExit("agent mismatch")
    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    role = info.user_role(agent.address)
    if role.get("role") != "agent" or role.get("data", {}).get("user", "").lower() != ACCOUNT.lower():
        raise SystemExit("agent authorization mismatch")
    exchange = Exchange(agent, constants.MAINNET_API_URL, account_address=ACCOUNT, timeout=30)
    exchange.update_leverage(1, COIN, True)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    session_started = int(time.time() * 1000)
    mids: deque[float] = deque(maxlen=31)
    tracker = MarkoutTracker()
    markout_store = MarkoutStore(MARKOUT_PATH)
    quality_store = QuoteQualityStore(QUALITY_PATH)
    seen: set[str] = set()
    safe_cycles = 0
    toxic_cycles = 0
    opening_gate = opening_gate_enabled() and strategy_reset_ack_enabled()
    risk_halted = False
    print(json.dumps({"event": "start", "coin": COIN, "strategy": STRATEGY_RESET_ACK, "opening_gate": opening_gate, "quality_path": QUALITY_PATH}), flush=True)

    try:
        while running:
            now = int(time.time() * 1000)
            position = zec_position(info)
            orders = zec_open_orders(info)
            mid = float(info.all_mids()[COIN])
            mids.append(mid)
            szi = float(position["szi"]) if position else 0.0
            upnl = float(position.get("unrealizedPnl", 0)) if position else 0.0
            fills = [fill for fill in info.user_fills_by_time(ACCOUNT, trailing_24h_start_ms()) if fill.get("coin") == COIN]
            observations = [item for item in (_fill_observation(fill) for fill in fills) if item is not None]
            for observation in observations:
                if observation.time_ms >= session_started and observation.trade_id not in seen:
                    seen.add(observation.trade_id)
                    tracker.add_fill(observation)
            produced = tracker.observe(now, mid)
            if produced:
                markout_store.record(produced, recorded_at_ms=now)

            toxicity = markout_store.summary(MARKOUT_HORIZON_MS, last_n=100, toxic_mean_bps=TOXIC_MARKOUT_MEAN_BPS, min_samples=TOXIC_MARKOUT_MIN_SAMPLES)
            buy_toxicity = markout_store.summary(MARKOUT_HORIZON_MS, side="buy", last_n=100, toxic_mean_bps=TOXIC_MARKOUT_MEAN_BPS, min_samples=TOXIC_MARKOUT_MIN_SAMPLES)
            sell_toxicity = markout_store.summary(MARKOUT_HORIZON_MS, side="sell", last_n=100, toxic_mean_bps=TOXIC_MARKOUT_MEAN_BPS, min_samples=TOXIC_MARKOUT_MIN_SAMPLES)

            net = session_net_pnl(fills, upnl)
            cooldown = post_fill_cooldown_active(fills, now)
            age = inventory_age_seconds(fills, now) if position else 0.0
            fast = fast_market_active(list(mids))
            medium = medium_trend_active(list(mids))
            history_ready = len(mids) >= 20

            book = info.l2_snapshot(COIN).get("levels", [])
            signal_l2 = depth_signal(book, depth_levels=5)
            best_bid = float(book[0][0]["px"])
            best_ask = float(book[1][0]["px"])
            bid_size = signal_l2.bid_depth
            ask_size = signal_l2.ask_depth
            imbalance, bid_buffer, ask_buffer, raw_bid, raw_ask = book_toxicity_signal(bid_size, ask_size)
            toxic_cycles = toxic_cycles + 1 if raw_bid or raw_ask else 0
            persistent_book_toxic = toxic_cycles >= TOXIC_CYCLES_TO_HALT

            current_opening = {int(order["oid"]): order for order in orders if not bool(order.get("reduceOnly", False))}
            tracked = quality_store.open_exposures()
            for order_id, exposure in tracked.items():
                if order_id not in current_opening:
                    quality_store.close(order_id, closed_at_ms=now, outcome="fill" if _exposure_filled(exposure, observations) else "cancel")
            for order_id, order in current_opening.items():
                if order_id in tracked:
                    continue
                side = "buy" if order.get("side") == "B" else "sell"
                price = float(order.get("limitPx", 0) or 0)
                size = float(order.get("sz", 0) or 0)
                created = int(order.get("timestamp", now) or now)
                quality_store.open(
                    Exposure(
                        order_id,
                        side,
                        price,
                        size,
                        created,
                        distance_from_touch_bps(best_bid, best_ask, side, price),
                        queue_ahead_size(book, side, price),
                    )
                )

            quality = quality_store.quality(last_n=200)
            buy_quality = quality_store.quality(side="buy", last_n=200)
            sell_quality = quality_store.quality(side="sell", last_n=200)
            learned_fill_multiplier = min(
                empirical_fill_multiplier(quality),
                max(0.35, (empirical_fill_multiplier(buy_quality) + empirical_fill_multiplier(sell_quality)) / 2.0),
            )

            volatility = rms_returns_bps(list(mids))
            direction = directional_pressure_bps(list(mids))
            edge = profitability_edge(
                volatility_bps=volatility,
                book_imbalance=signal_l2.imbalance,
                markout_mean_bps=toxicity.mean_markout_bps,
                markout_negative_rate=toxicity.negative_rate,
                buy_markout_mean_bps=buy_toxicity.mean_markout_bps,
                buy_markout_negative_rate=buy_toxicity.negative_rate,
                sell_markout_mean_bps=sell_toxicity.mean_markout_bps,
                sell_markout_negative_rate=sell_toxicity.negative_rate,
                directional_bps=direction,
                round_trip_fee_bps=ROUND_TRIP_MAKER_FEE_BPS,
                minimum_profit_bps=MINIMUM_NET_PROFIT_BPS,
                fill_size_multiplier=learned_fill_multiplier,
            )
            pause_bid = (persistent_book_toxic and raw_bid) or edge.pause_bid or buy_toxicity.toxic
            pause_ask = (persistent_book_toxic and raw_ask) or edge.pause_ask or sell_toxicity.toxic
            market_safe = history_ready and not fast and not medium and not (pause_bid and pause_ask)
            safe_cycles = safe_cycles + 1 if market_safe and not position else 0
            profit_locked = profit_lock_reached(net)
            adverse = inventory_adverse_move_bps(float(position["entryPx"]), mid, szi) if position else 0.0
            action = "none"

            if persistent_risk_halt_required(szi, net, profit_locked):
                risk_halted = True
            if position and inventory_hard_exit_required(upnl, age, adverse):
                cancel_orders(exchange, orders)
                flatten(exchange, info)
                action = "hard_exit"
            elif szi > 0:
                _, target = inventory_exit_quotes(float(position["entryPx"]), szi)
                target = best_ask if age >= SOFT_EXIT_AGE_SECONDS else target
                _, ask = passive_quotes(info, mid, target)
                if exit_order_matches(orders, False, abs(szi), ask):
                    action = "retain_exit"
                else:
                    cancel_orders(exchange, orders)
                    place(exchange, False, abs(szi), ask, True)
                    action = "replace_exit"
            elif szi < 0:
                target, _ = inventory_exit_quotes(float(position["entryPx"]), szi)
                target = best_bid if age >= SOFT_EXIT_AGE_SECONDS else target
                bid, _ = passive_quotes(info, target, mid)
                if exit_order_matches(orders, True, abs(szi), bid):
                    action = "retain_exit"
                else:
                    cancel_orders(exchange, orders)
                    place(exchange, True, abs(szi), bid, True)
                    action = "replace_exit"
            elif not opening_gate or risk_halted or cooldown or not market_safe or safe_cycles < SAFE_CYCLES_TO_QUOTE:
                if orders:
                    urgent = risk_halted or cooldown or fast or medium or (pause_bid and pause_ask)
                    if urgent or minimum_quote_lifetime_elapsed(orders):
                        cancel_orders(exchange, orders)
                        action = "cancel_unsafe"
                    else:
                        action = "retain_unsafe_grace"
            else:
                size = dynamic_order_size(BASE_ORDER_SIZE, LOT_SIZE, edge.size_multiplier)
                desired: list[tuple[bool, float, float]] = []
                if size < LOT_SIZE:
                    action = "edge_size_zero"
                else:
                    strategy_bid, strategy_ask = lighter_style_quotes(
                        best_bid,
                        best_ask,
                        bid_size,
                        ask_size,
                        szi,
                        MAX_NET_SIZE,
                        volatility,
                        bid_buffer + edge.bid_extra_bps,
                        ask_buffer + edge.ask_extra_bps,
                        required_edge_bps=edge.required_edge_bps,
                        fair_value=signal_l2.fair_value,
                    )
                    bid, ask = passive_quotes(info, strategy_bid, strategy_ask)
                    if not pause_bid:
                        desired.append((True, size, bid))
                    if not pause_ask:
                        desired.append((False, size, ask))
                    if mid * size > CAPITAL_BUDGET_USD:
                        desired = []
                        action = "capital_gate"
                if opening_orders_match(orders, desired):
                    action = "retain_quotes"
                elif orders and not opening_orders_hard_stale(orders, desired) and not minimum_quote_lifetime_elapsed(orders):
                    action = "retain_requote_grace"
                else:
                    cancel_orders(exchange, orders)
                    for buy, order_size, price in desired:
                        place(exchange, buy, order_size, price, False)
                    if desired:
                        action = "replace_quotes"

            print(
                json.dumps(
                    {
                        "event": "cycle",
                        "mid": mid,
                        "l2_fair": signal_l2.fair_value,
                        "l2_spread_bps": signal_l2.spread_bps,
                        "szi": szi,
                        "net_pnl": net,
                        "quote_action": action,
                        "volatility_bps": volatility,
                        "directional_bps": direction,
                        "book_imbalance": signal_l2.imbalance,
                        "required_edge_bps": edge.required_edge_bps,
                        "edge_score": edge.score,
                        "size_multiplier": edge.size_multiplier,
                        "learned_fill_multiplier": learned_fill_multiplier,
                        "pause_bid": pause_bid,
                        "pause_ask": pause_ask,
                        "markout_mean_bps": toxicity.mean_markout_bps,
                        "buy_markout_mean_bps": buy_toxicity.mean_markout_bps,
                        "sell_markout_mean_bps": sell_toxicity.mean_markout_bps,
                        "fill_samples": quality.samples,
                        "fill_rate": quality.fill_rate,
                        "buy_fill_rate": buy_quality.fill_rate,
                        "sell_fill_rate": sell_quality.fill_rate,
                        "queue_ahead_ratio": quality.mean_queue_ahead_ratio,
                    }
                ),
                flush=True,
            )
            for _ in range(REFRESH_SECONDS):
                if not running:
                    break
                time.sleep(1)
    finally:
        cancel_all(exchange, info)
        print(json.dumps({"event": "stopped"}), flush=True)


if __name__ == "__main__":
    main()
