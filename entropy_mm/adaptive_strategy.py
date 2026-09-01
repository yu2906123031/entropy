"""Shared adaptive-v4 quote construction for live and dry-run paths."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .edge import EdgeDecision, dynamic_order_size


@dataclass(frozen=True)
class AdaptiveQuotePair:
    bid: float
    ask: float
    size: float
    half_spread_bps: float


def reservation_price(fair: float, inventory: float, max_inventory: float, *, skew_bps_at_max: float, skew_power: float) -> float:
    if fair <= 0 or max_inventory <= 0 or skew_power <= 0:
        raise ValueError("invalid reservation-price inputs")
    ratio = max(-1.0, min(1.0, inventory / max_inventory))
    nonlinear = math.copysign(abs(ratio) ** skew_power, ratio) if ratio else 0.0
    return fair * (1.0 - nonlinear * skew_bps_at_max / 10_000.0)


def adaptive_quote_pair(
    *,
    best_bid: float,
    best_ask: float,
    fair_value: float,
    inventory: float,
    max_inventory: float,
    volatility_bps: float,
    bid_buffer_bps: float,
    ask_buffer_bps: float,
    edge: EdgeDecision,
    base_half_spread_bps: float,
    round_trip_fee_bps: float,
    minimum_profit_bps: float,
    base_size: float,
    lot_size: float,
    inventory_skew_bps: float = 20.0,
    inventory_skew_power: float = 1.5,
    fast_market_threshold_bps: float = 25.0,
) -> AdaptiveQuotePair:
    values = (
        best_bid,
        best_ask,
        fair_value,
        max_inventory,
        volatility_bps,
        bid_buffer_bps,
        ask_buffer_bps,
        base_half_spread_bps,
        round_trip_fee_bps,
        minimum_profit_bps,
        base_size,
        lot_size,
    )
    if any(not math.isfinite(value) for value in values) or best_bid <= 0 or best_ask <= best_bid:
        raise ValueError("invalid adaptive quote inputs")
    center = reservation_price(
        fair_value,
        inventory,
        max_inventory,
        skew_bps_at_max=inventory_skew_bps,
        skew_power=inventory_skew_power,
    )
    mid = (best_bid + best_ask) / 2.0
    observed_half_bps = (best_ask - best_bid) / mid * 5_000.0
    vol_ratio = max(0.0, volatility_bps / max(fast_market_threshold_bps, 1e-9))
    continuous_vol = volatility_bps * (1.0 + min(2.0, vol_ratio * vol_ratio))
    half = max(
        base_half_spread_bps,
        observed_half_bps + round_trip_fee_bps + minimum_profit_bps,
        edge.required_edge_bps,
        continuous_vol,
    )
    bid = min(center * (1.0 - (half + bid_buffer_bps + edge.bid_extra_bps) / 10_000.0), best_bid)
    ask = max(center * (1.0 + (half + ask_buffer_bps + edge.ask_extra_bps) / 10_000.0), best_ask)
    size = dynamic_order_size(base_size, lot_size, edge.size_multiplier)
    return AdaptiveQuotePair(bid, ask, size, half)
