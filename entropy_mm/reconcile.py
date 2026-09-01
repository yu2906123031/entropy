"""Deterministic order reconciliation for the Entropy market maker."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from .quote_model import Quote

@dataclass(frozen=True)
class LiveOrder:
    order_id: int
    side: str
    price: float
    size: float
    reduce_only: bool = False
    created_at_ms: int | None = None

@dataclass(frozen=True)
class Cancel:
    order_id: int
    reason: str

@dataclass(frozen=True)
class Place:
    level: int
    side: str
    price: float
    size: float
    post_only: bool = True
    reduce_only: bool = False
    quote_epoch: int = 0

@dataclass(frozen=True)
class ReconcilePlan:
    cancels: tuple[Cancel, ...]
    places: tuple[Place, ...]
    kept_order_ids: tuple[int, ...]

def _same_number(left: float, right: float) -> bool:
    return Decimal(str(left)) == Decimal(str(right))

def _distance_bps(order: LiveOrder, quote: Quote) -> float:
    return abs(order.price - quote.price) / quote.price * 10_000

def reconcile_orders(
    existing: Iterable[LiveOrder],
    desired: Iterable[Quote],
    *,
    now_ms: int | None = None,
    min_order_lifetime_ms: int = 0,
    reprice_threshold_bps: float = 0.0,
    hard_reprice_threshold_bps: float | None = None,
    quote_epoch: int = 0,
) -> ReconcilePlan:
    """Converge opening orders while never protecting a materially stale quote."""
    if min_order_lifetime_ms < 0 or reprice_threshold_bps < 0 or quote_epoch < 0:
        raise ValueError("reconciliation thresholds and quote epoch must be non-negative")
    hard = max(
        reprice_threshold_bps,
        hard_reprice_threshold_bps if hard_reprice_threshold_bps is not None else max(8.0, reprice_threshold_bps * 4),
    )
    remaining = list(desired)
    cancels: list[Cancel] = []
    kept: list[int] = []
    for order in existing:
        if order.reduce_only:
            kept.append(order.order_id)
            continue
        candidates = [(index, quote) for index, quote in enumerate(remaining) if quote.side == order.side and _same_number(quote.size, order.size)]
        match_index = next((index for index, quote in candidates if _distance_bps(order, quote) <= reprice_threshold_bps), None)
        age_ms = None if now_ms is None or order.created_at_ms is None else now_ms - order.created_at_ms
        is_young = age_ms is not None and 0 <= age_ms < min_order_lifetime_ms
        if match_index is None and is_young and candidates:
            best_index, best_quote = min(candidates, key=lambda item: _distance_bps(order, item[1]))
            if _distance_bps(order, best_quote) < hard:
                match_index = best_index
        if match_index is None:
            hard_stale = bool(candidates) and min(_distance_bps(order, quote) for _, quote in candidates) >= hard
            cancels.append(Cancel(order.order_id, "hard_stale" if hard_stale else "stale_or_duplicate"))
            continue
        remaining.pop(match_index)
        kept.append(order.order_id)
    places = tuple(Place(q.level, q.side, q.price, q.size, quote_epoch=quote_epoch) for q in remaining)
    return ReconcilePlan(tuple(cancels), places, tuple(kept))
