"""PnL attribution for maker fills and inventory exits."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ExpectancyAttribution:
    spread_capture_bps: float
    fee_bps: float
    adverse_selection_bps: float
    inventory_exit_bps: float
    funding_bps: float
    net_bps: float


def maker_expectancy(
    *,
    side: str,
    fill_price: float,
    fair_value: float,
    markout_bps: float = 0.0,
    fee_bps: float = 0.0,
    inventory_exit_bps: float = 0.0,
    funding_bps: float = 0.0,
) -> ExpectancyAttribution:
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if not all(math.isfinite(v) for v in (fill_price, fair_value, markout_bps, fee_bps, inventory_exit_bps, funding_bps)) or fill_price <= 0 or fair_value <= 0:
        raise ValueError("invalid expectancy inputs")
    raw = (fair_value / fill_price - 1.0) * 10_000.0
    spread = raw if side == "buy" else -raw
    adverse = min(0.0, markout_bps)
    net = spread - abs(fee_bps) + adverse - abs(inventory_exit_bps) - abs(funding_bps)
    return ExpectancyAttribution(spread, abs(fee_bps), adverse, abs(inventory_exit_bps), abs(funding_bps), net)


def aggregate_expectancy(items: list[ExpectancyAttribution]) -> ExpectancyAttribution:
    if not items:
        return ExpectancyAttribution(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    n = len(items)
    return ExpectancyAttribution(
        sum(x.spread_capture_bps for x in items) / n,
        sum(x.fee_bps for x in items) / n,
        sum(x.adverse_selection_bps for x in items) / n,
        sum(x.inventory_exit_bps for x in items) / n,
        sum(x.funding_bps for x in items) / n,
        sum(x.net_bps for x in items) / n,
    )
