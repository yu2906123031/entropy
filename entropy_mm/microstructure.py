"""L2 microstructure features for maker quote selection."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class DepthSignal:
    fair_value: float
    bid_depth: float
    ask_depth: float
    imbalance: float
    spread_bps: float


def _levels(rows: list[dict], limit: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows[:limit]:
        try:
            px = float(row.get("px", 0) or 0)
            size = float(row.get("sz", 0) or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(px) and math.isfinite(size) and px > 0 and size > 0:
            out.append((px, size))
    return out


def depth_signal(levels: list[list[dict]], *, depth_levels: int = 5, decay: float = 0.70, fair_weight: float = 0.65) -> DepthSignal:
    """Return a bounded depth-weighted fair value and aggregate depth imbalance.

    Farther levels receive exponentially smaller weights. The raw depth microprice
    is clamped inside the BBO, then blended with mid so one distorted level cannot
    move fair value excessively.
    """
    if len(levels) < 2 or depth_levels < 1 or not 0 < decay <= 1 or not 0 <= fair_weight <= 1:
        raise ValueError("invalid depth signal inputs")
    bids = _levels(levels[0], depth_levels)
    asks = _levels(levels[1], depth_levels)
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        raise ValueError("two-sided L2 book required")
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0

    def weighted(side: list[tuple[float, float]]) -> tuple[float, float]:
        weighted_size = 0.0
        weighted_notional = 0.0
        for index, (price, size) in enumerate(side):
            weight = decay ** index
            weighted_size += size * weight
            weighted_notional += price * size * weight
        return weighted_notional / weighted_size, weighted_size

    bid_vwap, bid_depth = weighted(bids)
    ask_vwap, ask_depth = weighted(asks)
    total_depth = bid_depth + ask_depth
    raw_fair = (ask_vwap * bid_depth + bid_vwap * ask_depth) / total_depth
    bounded_fair = max(best_bid, min(best_ask, raw_fair))
    fair = mid + fair_weight * (bounded_fair - mid)
    imbalance = (bid_depth - ask_depth) / total_depth
    spread_bps = (best_ask - best_bid) / mid * 10_000.0
    return DepthSignal(fair, bid_depth, ask_depth, max(-1.0, min(1.0, imbalance)), spread_bps)


def queue_ahead_size(levels: list[list[dict]], side: str, price: float, *, depth_levels: int = 20) -> float:
    """Approximate displayed quantity ahead of a passive order at ``price``."""
    if side not in {"buy", "sell"} or price <= 0 or len(levels) < 2:
        raise ValueError("invalid queue inputs")
    rows = _levels(levels[0] if side == "buy" else levels[1], depth_levels)
    if side == "buy":
        return sum(size for px, size in rows if px >= price)
    return sum(size for px, size in rows if px <= price)


def distance_from_touch_bps(best_bid: float, best_ask: float, side: str, price: float) -> float:
    if best_bid <= 0 or best_ask <= best_bid or price <= 0 or side not in {"buy", "sell"}:
        raise ValueError("invalid touch distance inputs")
    mid = (best_bid + best_ask) / 2.0
    if side == "buy":
        return max(0.0, best_bid - price) / mid * 10_000.0
    return max(0.0, price - best_ask) / mid * 10_000.0
