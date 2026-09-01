"""Profitability-aware quote gates for adaptive market making."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class EdgeDecision:
    required_edge_bps: float
    size_multiplier: float
    bid_extra_bps: float
    ask_extra_bps: float
    pause_bid: bool
    pause_ask: bool
    score: float


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def directional_pressure_bps(mids: list[float], lookback: int = 6) -> float:
    """Signed short-horizon displacement; positive means upward pressure."""
    if len(mids) < 2:
        return 0.0
    sample = mids[-max(2, lookback):]
    if sample[0] <= 0 or any(not math.isfinite(v) or v <= 0 for v in sample):
        return 0.0
    return (sample[-1] / sample[0] - 1.0) * 10_000.0


def profitability_edge(
    *,
    volatility_bps: float,
    book_imbalance: float,
    markout_mean_bps: float | None,
    markout_negative_rate: float | None,
    directional_bps: float,
    round_trip_fee_bps: float,
    minimum_profit_bps: float,
    max_directional_bps: float = 30.0,
) -> EdgeDecision:
    """Translate observed toxicity into spread, size and side-specific risk controls.

    The required edge increases when historical maker markout is adverse, current
    volatility rises, flow becomes directional, or the book is strongly imbalanced.
    Size shrinks continuously instead of waiting for a hard halt.
    """
    values = (volatility_bps, book_imbalance, directional_bps, round_trip_fee_bps, minimum_profit_bps)
    if any(not math.isfinite(v) for v in values):
        raise ValueError("edge inputs must be finite")
    adverse_markout = max(0.0, -(markout_mean_bps or 0.0))
    negative_rate = _clip(markout_negative_rate or 0.0, 0.0, 1.0)
    imbalance_mag = _clip(abs(book_imbalance), 0.0, 1.0)
    direction_mag = min(abs(directional_bps), max_directional_bps)

    toxicity_buffer = adverse_markout * 1.25 + max(0.0, negative_rate - 0.5) * 12.0
    volatility_buffer = max(0.0, volatility_bps) * 0.35
    imbalance_buffer = imbalance_mag * 8.0
    direction_buffer = direction_mag * 0.30
    required = round_trip_fee_bps + minimum_profit_bps + toxicity_buffer + volatility_buffer + imbalance_buffer + direction_buffer

    score = toxicity_buffer + volatility_buffer + imbalance_buffer + direction_buffer
    size_multiplier = _clip(1.0 / (1.0 + score / 12.0), 0.20, 1.0)

    # Upward pressure makes asks dangerous; downward pressure makes bids dangerous.
    directional_extra = min(18.0, direction_mag * 0.60)
    bid_extra = directional_extra if directional_bps < 0 else 0.0
    ask_extra = directional_extra if directional_bps > 0 else 0.0

    # Book imbalance is interpreted conservatively: large bid depth can make asks
    # vulnerable to upward pressure, large ask depth can make bids vulnerable.
    imbalance_extra = min(12.0, imbalance_mag * 12.0)
    if book_imbalance > 0:
        ask_extra += imbalance_extra
    elif book_imbalance < 0:
        bid_extra += imbalance_extra

    pause_bid = directional_bps <= -max_directional_bps or (book_imbalance <= -0.90 and volatility_bps >= 5.0)
    pause_ask = directional_bps >= max_directional_bps or (book_imbalance >= 0.90 and volatility_bps >= 5.0)
    if adverse_markout >= 8.0 and negative_rate >= 0.75:
        pause_bid = True
        pause_ask = True

    return EdgeDecision(required, size_multiplier, bid_extra, ask_extra, pause_bid, pause_ask, score)


def dynamic_order_size(base_size: float, lot_size: float, multiplier: float) -> float:
    if base_size <= 0 or lot_size <= 0 or not math.isfinite(multiplier):
        raise ValueError("invalid dynamic size inputs")
    units = math.floor(base_size * _clip(multiplier, 0.0, 1.0) / lot_size + 1e-12)
    return max(0.0, units * lot_size)
