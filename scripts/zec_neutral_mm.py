#!/usr/bin/env python3
"""Bounded one-level ZEC neutral market-making test using unified USDC margin."""
from __future__ import annotations

import getpass
import json
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

ACCOUNT = "0x78605485604BA45ce0eF860DB1594ec810154477"
AGENT = "0x3B4347B99BB749eBdD6DE720736796E1b7Dfe4a6"
COIN = "ZEC"
LEVEL_SIZES = (0.02,)
LEVEL_SPREAD_BPS = (35.0,)
CAPITAL_BUDGET_USD = 20.0
MAX_NET_SIZE = 0.0201
MAX_LOSS_USD = 1.0
REFRESH_SECONDS = 2
REQUOTE_THRESHOLD_BPS = 5.0
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
SOFT_EXIT_AGE_SECONDS = 15
MAX_INVENTORY_AGE_SECONDS = 35
MAX_INVENTORY_LOSS_USD = 0.035
MAX_ADVERSE_MOVE_BPS = 8.0
DAILY_PROFIT_LOCK_USD = 0.10
MARKET_CLOSE_SLIPPAGE = 0.005
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
        p = item.get("position", {})
        if p.get("coin") == COIN and abs(float(p.get("szi", 0) or 0)) > 0:
            return p
    return None


def zec_open_orders(info: Info) -> list[dict]:
    return [order for order in info.frontend_open_orders(ACCOUNT) if order.get("coin") == COIN]


def cancel_orders(exchange: Exchange, orders: list[dict]) -> None:
    for order in orders:
        result = exchange.cancel(COIN, int(order["oid"]))
        print(json.dumps({"event": "cancel", "oid": order["oid"], "result": result}), flush=True)


def cancel_all(exchange: Exchange, info: Info) -> None:
    cancel_orders(exchange, zec_open_orders(info))


def order_matches(
    order: dict,
    buy: bool,
    size: float,
    price: float,
    reduce_only: bool,
    threshold_bps: float = REQUOTE_THRESHOLD_BPS,
) -> bool:
    side_matches = order.get("side") == ("B" if buy else "A")
    size_matches = abs(float(order.get("sz", 0) or 0) - size) < 1e-9
    reduce_matches = bool(order.get("reduceOnly", False)) == reduce_only
    live_price = float(order.get("limitPx", 0) or 0)
    price_matches = price > 0 and abs(live_price / price - 1.0) * 10_000 < threshold_bps
    return side_matches and size_matches and reduce_matches and price_matches


def quote_pair_matches(orders: list[dict], size: float, bid: float, ask: float) -> bool:
    return len(orders) == 2 and any(order_matches(o, True, size, bid, False) for o in orders) and any(
        order_matches(o, False, size, ask, False) for o in orders
    )


def opening_orders_match(orders: list[dict], desired: list[tuple[bool, float, float]]) -> bool:
    """Match the exact opening set, including a one-sided toxicity pause."""
    return len(orders) == len(desired) and all(
        any(order_matches(order, buy, size, price, False) for order in orders)
        for buy, size, price in desired
    )


def minimum_quote_lifetime_elapsed(
    orders: list[dict],
    now_ms: int | None = None,
    minimum_seconds: float = MINIMUM_QUOTE_LIFETIME_SECONDS,
) -> bool:
    """Keep timestamped opening quotes alive long enough to earn queue priority."""
    if not orders:
        return True
    timestamps = [int(order.get("timestamp", 0) or 0) for order in orders]
    if any(timestamp <= 0 for timestamp in timestamps):
        return True
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return all(current_ms - timestamp >= minimum_seconds * 1000 for timestamp in timestamps)


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
        print(json.dumps({"event": "post_only_retry", "side": "buy" if buy else "sell", "error": error}), flush=True)
        return False
    raise RuntimeError(f"order rejected: {status}")


def passive_quotes(info: Info, strategy_bid: float, strategy_ask: float) -> tuple[float, float]:
    """Clamp strategy quotes to the current passive side of the live BBO."""
    levels = info.l2_snapshot(COIN).get("levels", [])
    if len(levels) < 2 or not levels[0] or not levels[1]:
        raise RuntimeError("ZEC order book has no two-sided BBO")
    best_bid = Decimal(str(levels[0][0]["px"]))
    best_ask = Decimal(str(levels[1][0]["px"]))
    tick = Decimal("0.1")
    safe_bid = min(Decimal(str(strategy_bid)), best_bid).quantize(tick, rounding=ROUND_FLOOR)
    safe_ask = max(Decimal(str(strategy_ask)), best_ask).quantize(tick, rounding=ROUND_CEILING)
    return float(safe_bid), float(safe_ask)


def inventory_exit_quotes(
    entry_price: float,
    szi: float,
    round_trip_fee_bps: float = ROUND_TRIP_MAKER_FEE_BPS,
    minimum_profit_bps: float = MINIMUM_NET_PROFIT_BPS,
) -> tuple[float | None, float | None]:
    """Return a cost-aware reduce-only target for the current inventory."""
    edge = (Decimal(str(round_trip_fee_bps)) + Decimal(str(minimum_profit_bps))) / Decimal("10000")
    entry = Decimal(str(entry_price))
    if szi > 0:
        return None, float(entry * (Decimal("1") + edge))
    if szi < 0:
        return float(entry * (Decimal("1") - edge)), None
    return None, None


def inventory_exit_strategy_price(
    entry_price: float,
    szi: float,
    best_bid: float,
    best_ask: float,
    age_seconds: float,
) -> float:
    """Use the profit target first, then join the passive touch to shed aging inventory."""
    target_bid, target_ask = inventory_exit_quotes(entry_price, szi)
    if age_seconds >= SOFT_EXIT_AGE_SECONDS:
        return best_ask if szi > 0 else best_bid
    target = target_ask if szi > 0 else target_bid
    if target is None:
        raise ValueError("inventory exit requires a non-zero position")
    return target


def adaptive_spread_bps(
    mids: list[float],
    base_spread_bps: float,
    round_trip_fee_bps: float = ROUND_TRIP_MAKER_FEE_BPS,
    minimum_profit_bps: float = MINIMUM_NET_PROFIT_BPS,
    volatility_multiplier: float = VOLATILITY_SPREAD_MULTIPLIER,
) -> float:
    """Return a fee-safe spread widened by observed short-horizon volatility."""
    cost_floor = round_trip_fee_bps + minimum_profit_bps
    if len(mids) < 2:
        return max(base_spread_bps, cost_floor)
    returns_bps = [
        abs((current / previous - 1.0) * 10_000)
        for previous, current in zip(mids, mids[1:])
        if previous > 0
    ]
    volatility_bps = statistics.fmean(returns_bps) * volatility_multiplier if returns_bps else 0.0
    return max(base_spread_bps, cost_floor, volatility_bps)


def rms_returns_bps(mids: list[float]) -> float:
    returns = [(current / previous - 1.0) * 10_000 for previous, current in zip(mids, mids[1:]) if previous > 0]
    return (sum(value * value for value in returns) / len(returns)) ** 0.5 if returns else 0.0


def microprice(best_bid: float, best_ask: float, bid_size: float, ask_size: float) -> float:
    if best_bid <= 0 or best_ask <= best_bid:
        raise ValueError("invalid two-sided book")
    total = bid_size + ask_size
    if bid_size <= 0 or ask_size <= 0 or total <= 0:
        return (best_bid + best_ask) / 2.0
    return (best_ask * bid_size + best_bid * ask_size) / total


def reservation_price(
    fair: float,
    inventory: float,
    max_inventory: float,
    skew_bps_at_max: float = INVENTORY_SKEW_BPS_AT_MAX,
) -> float:
    ratio = max(-1.0, min(1.0, inventory / max_inventory)) if max_inventory > 0 else 0.0
    return fair * (1.0 - ratio * skew_bps_at_max / 10_000.0)


def book_toxicity_signal(
    bid_size: float,
    ask_size: float,
    widen_threshold: float = TOXIC_WIDEN_THRESHOLD,
    pause_threshold: float = TOXIC_PAUSE_THRESHOLD,
    max_buffer_bps: float = MAX_TOXIC_BUFFER_BPS,
) -> tuple[float, float, float, bool, bool]:
    total = bid_size + ask_size
    if bid_size <= 0 or ask_size <= 0 or total <= 0:
        return 0.0, 0.0, 0.0, False, False
    imbalance = max(-1.0, min(1.0, (bid_size - ask_size) / total))
    magnitude = abs(imbalance)
    span = max(1e-9, pause_threshold - widen_threshold)
    buffer = 0.0 if magnitude <= widen_threshold else max_buffer_bps * min(1.0, (magnitude - widen_threshold) / span)
    bid_toxic = imbalance < 0
    ask_toxic = imbalance > 0
    return (
        imbalance,
        buffer if bid_toxic else 0.0,
        buffer if ask_toxic else 0.0,
        bid_toxic and magnitude >= pause_threshold,
        ask_toxic and magnitude >= pause_threshold,
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
) -> tuple[float, float]:
    """Lighter microprice/reservation-price model adapted to one Hyperliquid level."""
    fair = microprice(best_bid, best_ask, bid_size, ask_size)
    center = reservation_price(fair, inventory, max_inventory)
    mid = (best_bid + best_ask) / 2.0
    observed_half_bps = (best_ask - best_bid) / mid * 5_000.0
    cost_floor = observed_half_bps + ROUND_TRIP_MAKER_FEE_BPS + MINIMUM_NET_PROFIT_BPS
    half_spread = max(base_half_spread_bps, cost_floor, volatility_bps * VOLATILITY_SPREAD_MULTIPLIER)
    bid = min(center * (1.0 - (half_spread + bid_adverse_buffer_bps) / 10_000.0), best_bid)
    ask = max(center * (1.0 + (half_spread + ask_adverse_buffer_bps) / 10_000.0), best_ask)
    return bid, ask


def directional_efficiency(mids: list[float]) -> float:
    """Return net displacement divided by total path; 0 means chop, 1 means one-way trend."""
    if len(mids) < 2 or any(mid <= 0 for mid in mids):
        return 0.0
    path_bps = sum(abs((current / previous - 1.0) * 10_000) for previous, current in zip(mids, mids[1:]))
    if path_bps <= 0:
        return 0.0
    net_bps = abs((mids[-1] / mids[0] - 1.0) * 10_000)
    return min(1.0, net_bps / path_bps)


def fast_market_active(
    mids: list[float],
    threshold_bps: float = FAST_MARKET_THRESHOLD_BPS,
    trend_efficiency: float = 0.70,
) -> bool:
    """Pause only fast directional movement; choppy volatility remains quoteable."""
    if len(mids) < 3 or mids[0] <= 0:
        return False
    net_move_bps = abs((mids[-1] / mids[0] - 1.0) * 10_000)
    return net_move_bps >= threshold_bps and directional_efficiency(mids) >= trend_efficiency


def medium_trend_active(
    mids: list[float],
    threshold_bps: float = MEDIUM_TREND_THRESHOLD_BPS,
    trend_efficiency: float = 0.55,
) -> bool:
    """Block fresh inventory only when the roughly five-minute move is directional."""
    if len(mids) < 20 or mids[0] <= 0:
        return False
    net_move_bps = abs((mids[-1] / mids[0] - 1.0) * 10_000)
    return net_move_bps >= threshold_bps and directional_efficiency(mids) >= trend_efficiency


def toxic_order_book(bid_size: float, ask_size: float, threshold: float = TOXIC_BOOK_IMBALANCE) -> bool:
    total = bid_size + ask_size
    if total <= 0:
        return True
    imbalance = bid_size / total
    return imbalance >= threshold or imbalance <= 1.0 - threshold


def top_book_sizes(info: Info, depth_levels: int = 5) -> tuple[float, float]:
    """Use several price levels so one small top-level update cannot flip the toxicity gate."""
    levels = info.l2_snapshot(COIN).get("levels", [])
    if len(levels) < 2 or not levels[0] or not levels[1]:
        raise RuntimeError("ZEC order book has no two-sided liquidity")
    bid_depth = sum(float(level.get("sz", 0) or 0) for level in levels[0][:depth_levels])
    ask_depth = sum(float(level.get("sz", 0) or 0) for level in levels[1][:depth_levels])
    return bid_depth, ask_depth


def inventory_hard_exit_required(
    unrealized_pnl: float,
    age_seconds: float,
    adverse_move_bps: float = 0.0,
    max_loss_usd: float = MAX_INVENTORY_LOSS_USD,
    max_age_seconds: float = MAX_INVENTORY_AGE_SECONDS,
    max_adverse_move_bps: float = MAX_ADVERSE_MOVE_BPS,
) -> bool:
    return (
        unrealized_pnl <= -abs(max_loss_usd)
        or age_seconds >= max_age_seconds
        or adverse_move_bps >= max_adverse_move_bps
    )


def inventory_adverse_move_bps(entry_price: float, mark_price: float, szi: float) -> float:
    if entry_price <= 0 or szi == 0:
        return 0.0
    signed_return_bps = (mark_price / entry_price - 1.0) * 10_000
    return max(0.0, -signed_return_bps if szi > 0 else signed_return_bps)


def inventory_age_seconds(fills: list[dict], now_ms: int | None = None) -> float:
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    latest_fill_ms = max((int(fill.get("time", 0) or 0) for fill in fills), default=current_ms)
    return max(0.0, (current_ms - latest_fill_ms) / 1000.0)


def opening_gate_enabled() -> bool:
    """Require an explicit operator gate before any risk-increasing order."""
    return os.getenv("ENTROPY_ALLOW_NEW_OPENINGS", "false").lower() == "true"


def post_fill_cooldown_active(fills: list[dict], now_ms: int | None = None) -> bool:
    if not fills:
        return False
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    latest_fill_ms = max(int(fill.get("time", 0) or 0) for fill in fills)
    return current_ms - latest_fill_ms < POST_FILL_COOLDOWN_SECONDS * 1000


def session_net_pnl(fills: list[dict], unrealized_pnl: float = 0.0) -> float:
    """Net PnL after fees, including current unrealized inventory PnL."""
    return sum(float(fill.get("closedPnl", 0) or 0) - float(fill.get("fee", 0) or 0) for fill in fills) + unrealized_pnl


def loss_limit_reached(net_pnl: float, limit_usd: float = MAX_LOSS_USD) -> bool:
    return net_pnl <= -abs(limit_usd)


def persistent_risk_halt_required(
    szi: float,
    net_pnl: float,
    profit_locked: bool,
    max_net_size: float = MAX_NET_SIZE,
    max_loss_usd: float = MAX_LOSS_USD,
) -> bool:
    """Reserve the sticky halt for account/session-level risk limits."""
    return abs(szi) > max_net_size or loss_limit_reached(net_pnl, max_loss_usd) or profit_locked


def profit_lock_reached(net_pnl: float, target_usd: float = DAILY_PROFIT_LOCK_USD) -> bool:
    return net_pnl >= abs(target_usd)


def trailing_24h_start_ms() -> int:
    return int(time.time() * 1000) - 24 * 60 * 60 * 1000


def flatten(exchange: Exchange, info: Info) -> None:
    p = zec_position(info)
    if p:
        szi = float(p["szi"])
        result = exchange.market_close(COIN, abs(szi), None, MARKET_CLOSE_SLIPPAGE, builder=BUILDER)
        print(json.dumps({"event": "flatten", "szi": szi, "result": result}), flush=True)


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
    print(json.dumps({"event": "start", "coin": COIN, "capital_budget_usd": CAPITAL_BUDGET_USD, "level_sizes": LEVEL_SIZES, "max_net_size": MAX_NET_SIZE, "max_loss_usd": MAX_LOSS_USD, "leverage": 1}), flush=True)
    recent_mids: deque[float] = deque(maxlen=31)
    safe_cycles = 0
    toxic_cycles = 0
    opening_gate = opening_gate_enabled()
    risk_halted = not opening_gate
    if not opening_gate:
        print(json.dumps({"event": "opening_gate_locked"}), flush=True)
    try:
        while running:
            p = zec_position(info)
            orders = zec_open_orders(info)
            mid = float(info.all_mids()[COIN])
            recent_mids.append(mid)
            opening_size = sum(LEVEL_SIZES)
            if mid * opening_size > CAPITAL_BUDGET_USD:
                raise RuntimeError(f"one-sided notional {mid * opening_size:.4f} exceeds capital budget")
            szi = float(p["szi"]) if p else 0.0
            upnl = float(p.get("unrealizedPnl", 0)) if p else 0.0
            fills = [fill for fill in info.user_fills_by_time(ACCOUNT, trailing_24h_start_ms()) if fill.get("coin") == COIN]
            net_pnl = session_net_pnl(fills, upnl)
            cooldown_active = post_fill_cooldown_active(fills)
            inventory_age = inventory_age_seconds(fills) if p else 0.0
            fast_market = fast_market_active(list(recent_mids))
            history_ready = len(recent_mids) >= 20
            medium_trend = medium_trend_active(list(recent_mids))
            book = info.l2_snapshot(COIN).get("levels", [])
            if len(book) < 2 or not book[0] or not book[1]:
                raise RuntimeError("ZEC order book has no two-sided liquidity")
            best_bid = float(book[0][0]["px"])
            best_ask = float(book[1][0]["px"])
            bid_size = sum(float(level.get("sz", 0) or 0) for level in book[0][:5])
            ask_size = sum(float(level.get("sz", 0) or 0) for level in book[1][:5])
            imbalance, bid_buffer_bps, ask_buffer_bps, raw_pause_bid, raw_pause_ask = book_toxicity_signal(
                bid_size, ask_size
            )
            raw_toxic_book = raw_pause_bid or raw_pause_ask
            toxic_cycles = toxic_cycles + 1 if raw_toxic_book else 0
            toxic_book = toxic_cycles >= TOXIC_CYCLES_TO_HALT
            pause_bid = toxic_book and raw_pause_bid
            pause_ask = toxic_book and raw_pause_ask
            market_safe = history_ready and not fast_market and not medium_trend
            safe_cycles = safe_cycles + 1 if market_safe and not p else 0
            profit_locked = profit_lock_reached(net_pnl)
            adverse_move_bps = inventory_adverse_move_bps(float(p["entryPx"]), mid, szi) if p else 0.0
            quote_action = "none"
            if persistent_risk_halt_required(szi, net_pnl, profit_locked):
                risk_halted = True
                print(json.dumps({"event": "risk_halt", "szi": szi, "upnl": upnl, "daily_net_pnl": net_pnl, "profit_locked": profit_locked}), flush=True)
            if p and inventory_hard_exit_required(upnl, inventory_age, adverse_move_bps):
                cancel_orders(exchange, orders)
                print(json.dumps({"event": "inventory_hard_exit", "szi": szi, "upnl": upnl, "inventory_age_seconds": inventory_age, "adverse_move_bps": adverse_move_bps}), flush=True)
                flatten(exchange, info)
                quote_action = "hard_exit"
            elif szi > 0:
                assert p is not None
                entry = float(p["entryPx"])
                target_ask = inventory_exit_strategy_price(entry, szi, best_bid, best_ask, inventory_age)
                _, ask = passive_quotes(info, mid, target_ask)
                if exit_order_matches(orders, False, abs(szi), ask):
                    quote_action = "retain_exit"
                else:
                    cancel_orders(exchange, orders)
                    place(exchange, False, abs(szi), ask, True)
                    quote_action = "replace_exit"
            elif szi < 0:
                assert p is not None
                entry = float(p["entryPx"])
                target_bid = inventory_exit_strategy_price(entry, szi, best_bid, best_ask, inventory_age)
                bid, _ = passive_quotes(info, target_bid, mid)
                if exit_order_matches(orders, True, abs(szi), bid):
                    quote_action = "retain_exit"
                else:
                    cancel_orders(exchange, orders)
                    place(exchange, True, abs(szi), bid, True)
                    quote_action = "replace_exit"
            elif risk_halted or cooldown_active or not market_safe or safe_cycles < SAFE_CYCLES_TO_QUOTE:
                if orders:
                    urgent_cancel = risk_halted or cooldown_active or fast_market or medium_trend
                    if urgent_cancel or minimum_quote_lifetime_elapsed(orders):
                        cancel_orders(exchange, orders)
                        quote_action = "cancel_unsafe"
                    else:
                        quote_action = "retain_unsafe_grace"
            else:
                size = LEVEL_SIZES[0]
                volatility_bps = rms_returns_bps(list(recent_mids))
                strategy_bid, strategy_ask = lighter_style_quotes(
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    inventory=szi,
                    max_inventory=MAX_NET_SIZE,
                    volatility_bps=volatility_bps,
                    bid_adverse_buffer_bps=bid_buffer_bps,
                    ask_adverse_buffer_bps=ask_buffer_bps,
                )
                bid, ask = passive_quotes(info, strategy_bid, strategy_ask)
                desired = []
                if not pause_bid:
                    desired.append((True, size, bid))
                if not pause_ask:
                    desired.append((False, size, ask))
                if opening_orders_match(orders, desired):
                    quote_action = "retain_quotes"
                elif orders and not minimum_quote_lifetime_elapsed(orders):
                    quote_action = "retain_requote_grace"
                else:
                    cancel_orders(exchange, orders)
                    for buy, desired_size, price in desired:
                        place(exchange, buy, desired_size, price, False)
                    quote_action = "replace_quotes"
            print(json.dumps({"event": "cycle", "mid": mid, "szi": szi, "upnl": upnl, "rolling_24h_net_pnl": net_pnl, "risk_halted": risk_halted, "profit_locked": profit_locked, "cooldown_active": cooldown_active, "opening_gate": opening_gate, "history_ready": history_ready, "safe_cycles": safe_cycles, "quote_action": quote_action, "fast_market": fast_market, "medium_trend": medium_trend, "raw_toxic_book": raw_toxic_book, "toxic_book": toxic_book, "book_imbalance": imbalance, "pause_bid": pause_bid, "pause_ask": pause_ask, "bid_buffer_bps": bid_buffer_bps, "ask_buffer_bps": ask_buffer_bps, "rms_volatility_bps": rms_returns_bps(list(recent_mids)), "inventory_age_seconds": inventory_age, "adverse_move_bps": adverse_move_bps}), flush=True)
            for _ in range(REFRESH_SECONDS):
                if not running:
                    break
                time.sleep(1)
    finally:
        cancel_all(exchange, info)
        print(json.dumps({"event": "stopped"}), flush=True)


if __name__ == "__main__":
    main()
