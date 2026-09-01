"""Single-cycle risk gate and quote planner for Entropy."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .quote_model import Book, Inventory, Quote, QuoteConfig, RiskLimits, build_quotes
from .reconcile import LiveOrder, ReconcilePlan, reconcile_orders


class RiskMode(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    SHOCK = "shock"
    HALTED = "halted"


@dataclass(frozen=True)
class EngineConfig:
    max_book_age_ms: int = 3_000
    position_mismatch_tolerance: float = 0.001
    max_margin_usage_ratio: float = 0.50
    daily_loss_limit: float = 10.0
    min_order_lifetime_ms: int = 5_000
    reprice_threshold_bps: float = 2.0


@dataclass(frozen=True)
class CycleInput:
    now_ms: int
    book_time_ms: int
    book: Book
    venue_inventory: Inventory
    ledger_inventory: Inventory
    open_orders: tuple[LiveOrder, ...] = ()
    volatility_bps: float = 0.0
    margin_usage_ratio: float = 0.0
    daily_pnl: float = 0.0


@dataclass(frozen=True)
class CycleDecision:
    mode: RiskMode
    reason: str
    quotes: tuple[Quote, ...]
    plan: ReconcilePlan


def _inventory_mismatch(left: Inventory, right: Inventory) -> float:
    return max(abs(left.long - right.long), abs(left.short - right.short))


def _cancel_opening_orders(cycle: CycleInput) -> ReconcilePlan:
    return reconcile_orders(cycle.open_orders, ())


def plan_cycle(
    cycle: CycleInput,
    quote_config: QuoteConfig,
    risk_limits: RiskLimits,
    engine_config: EngineConfig = EngineConfig(),
) -> CycleDecision:
    """Apply fail-closed risk gates, build quotes, then reconcile orders."""
    book_age_ms = cycle.now_ms - cycle.book_time_ms
    if book_age_ms < 0 or book_age_ms > engine_config.max_book_age_ms:
        return CycleDecision(RiskMode.HALTED, "stale_book", (), _cancel_opening_orders(cycle))
    if _inventory_mismatch(cycle.venue_inventory, cycle.ledger_inventory) > engine_config.position_mismatch_tolerance:
        return CycleDecision(RiskMode.HALTED, "position_mismatch", (), _cancel_opening_orders(cycle))
    if cycle.margin_usage_ratio >= engine_config.max_margin_usage_ratio:
        return CycleDecision(RiskMode.HALTED, "margin_limit", (), _cancel_opening_orders(cycle))
    if cycle.daily_pnl <= -engine_config.daily_loss_limit:
        return CycleDecision(RiskMode.HALTED, "daily_loss_limit", (), _cancel_opening_orders(cycle))
    if cycle.volatility_bps >= quote_config.shock_vol_bps:
        return CycleDecision(RiskMode.SHOCK, "volatility_shock", (), _cancel_opening_orders(cycle))

    mode = RiskMode.ELEVATED if cycle.volatility_bps >= quote_config.elevated_vol_bps else RiskMode.NORMAL
    quotes = tuple(
        build_quotes(
            cycle.book,
            cycle.venue_inventory,
            risk_limits,
            quote_config,
            cycle.volatility_bps,
        )
    )
    plan = reconcile_orders(
        cycle.open_orders,
        quotes,
        now_ms=cycle.now_ms,
        min_order_lifetime_ms=engine_config.min_order_lifetime_ms,
        reprice_threshold_bps=engine_config.reprice_threshold_bps,
    )
    return CycleDecision(mode, "quotes_active", quotes, plan)
