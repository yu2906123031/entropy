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
) -> ReconcilePlan:
    """Produce cancel-first actions that converge opening orders to desired quotes.

    Reduce-only orders belong to the exit engine and are preserved. Opening
    orders inside the reprice threshold or minimum lifetime are also preserved.
    """
    if min_order_lifetime_ms < 0 or reprice_threshold_bps < 0:
        raise ValueError("reconciliation thresholds must be non-negative")

    remaining = list(desired)
    cancels: list[Cancel] = []
    kept: list[int] = []

    for order in existing:
        if order.reduce_only:
            kept.append(order.order_id)
            continue
        candidates = [
            (index, quote)
            for index, quote in enumerate(remaining)
            if quote.side == order.side and _same_number(quote.size, order.size)
        ]
        match_index = next(
            (index for index, quote in candidates if _distance_bps(order, quote) <= reprice_threshold_bps),
            None,
        )
        is_young = (
            now_ms is not None
            and order.created_at_ms is not None
            and now_ms - order.created_at_ms < min_order_lifetime_ms
        )
        if match_index is None and is_young and candidates:
            match_index = min(candidates, key=lambda item: _distance_bps(order, item[1]))[0]
        if match_index is None:
            cancels.append(Cancel(order.order_id, "stale_or_duplicate"))
            continue
        remaining.pop(match_index)
        kept.append(order.order_id)

    places = tuple(
        Place(level=quote.level, side=quote.side, price=quote.price, size=quote.size)
        for quote in remaining
    )
    return ReconcilePlan(tuple(cancels), places, tuple(kept))
