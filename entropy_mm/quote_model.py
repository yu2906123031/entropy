"""Pure inventory-aware market-making quote model for Entropy."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math


@dataclass(frozen=True)
class Book:
    bid: float
    ask: float
    bid_size: float
    ask_size: float


@dataclass(frozen=True)
class Inventory:
    long: float = 0.0
    short: float = 0.0

    @property
    def net(self) -> float:
        return self.long - self.short

    @property
    def gross(self) -> float:
        return self.long + self.short


@dataclass(frozen=True)
class RiskLimits:
    max_long: float
    max_short: float
    max_net: float
    max_gross: float


@dataclass(frozen=True)
class QuoteConfig:
    tick_size: float
    lot_size: float
    layers: int = 3
    base_half_spread_bps: float = 8.0
    level_gap_bps: float = 6.0
    inventory_skew_bps: float = 12.0
    order_size: float = 0.2
    elevated_vol_bps: float = 20.0
    shock_vol_bps: float = 50.0
    elevated_spread_multiplier: float = 1.8
    elevated_size_multiplier: float = 0.5


@dataclass(frozen=True)
class Quote:
    level: int
    side: str
    price: float
    size: float


def _finite_positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def validate(book: Book, inventory: Inventory, limits: RiskLimits, cfg: QuoteConfig) -> None:
    for name, value in vars(book).items():
        _finite_positive(name, value)
    if book.bid >= book.ask:
        raise ValueError("book must have bid below ask")
    for name, value in vars(limits).items():
        _finite_positive(name, value)
    for name in ("tick_size", "lot_size", "order_size"):
        _finite_positive(name, getattr(cfg, name))
    if not isinstance(cfg.layers, int) or cfg.layers < 1:
        raise ValueError("layers must be a positive integer")
    if inventory.long < 0 or inventory.short < 0:
        raise ValueError("inventory magnitudes must be non-negative")


def microprice(book: Book) -> float:
    _finite_positive("bid", book.bid)
    _finite_positive("ask", book.ask)
    _finite_positive("bid_size", book.bid_size)
    _finite_positive("ask_size", book.ask_size)
    if book.bid >= book.ask:
        raise ValueError("book must have bid below ask")
    return (book.ask * book.bid_size + book.bid * book.ask_size) / (book.bid_size + book.ask_size)


def _floor_step(value: float, step: float) -> float:
    result = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)
    return float(result * Decimal(str(step)))


def _ceil_step(value: float, step: float) -> float:
    result = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_CEILING)
    return float(result * Decimal(str(step)))


def opening_capacities(inventory: Inventory, limits: RiskLimits) -> tuple[float, float]:
    """Return worst-case additional long and short capacity.

    Each side is bounded independently by side, net, and shared gross limits.
    The shared gross remainder is divided equally when both sides can open.
    """
    gross_remaining = max(0.0, limits.max_gross - inventory.gross)
    long_cap = max(0.0, min(
        limits.max_long - inventory.long,
        limits.max_net - inventory.net,
        gross_remaining,
    ))
    short_cap = max(0.0, min(
        limits.max_short - inventory.short,
        limits.max_net + inventory.net,
        gross_remaining,
    ))
    if long_cap > 0 and short_cap > 0 and long_cap + short_cap > gross_remaining:
        allocation = gross_remaining / 2
        long_cap = min(long_cap, allocation)
        short_cap = min(short_cap, allocation)
    return long_cap, short_cap


def build_quotes(
    book: Book,
    inventory: Inventory,
    limits: RiskLimits,
    cfg: QuoteConfig,
    volatility_bps: float = 0.0,
) -> list[Quote]:
    """Build Post-Only, inventory-aware opening quotes.

    Elevated volatility widens and shrinks quotes. Shock volatility suppresses
    opening quotes so the execution layer can retain reduce-only exits.
    """
    validate(book, inventory, limits, cfg)
    if not math.isfinite(volatility_bps) or volatility_bps < 0:
        raise ValueError("volatility_bps must be finite and non-negative")
    if volatility_bps >= cfg.shock_vol_bps:
        return []

    fair = microprice(book)
    inventory_ratio = max(-1.0, min(1.0, inventory.net / limits.max_net))
    reservation = fair * (1 - inventory_ratio * cfg.inventory_skew_bps / 10_000)
    spread_multiplier = cfg.elevated_spread_multiplier if volatility_bps >= cfg.elevated_vol_bps else 1.0
    size_multiplier = cfg.elevated_size_multiplier if volatility_bps >= cfg.elevated_vol_bps else 1.0
    layer_count = max(1, cfg.layers - 1) if volatility_bps >= cfg.elevated_vol_bps else cfg.layers
    half_spread = fair * cfg.base_half_spread_bps * spread_multiplier / 10_000
    gap = fair * cfg.level_gap_bps / 10_000

    long_cap, short_cap = opening_capacities(inventory, limits)
    desired_size = _floor_step(cfg.order_size * size_multiplier, cfg.lot_size)
    quotes: list[Quote] = []
    for level in range(layer_count):
        bid_raw = min(reservation - half_spread - level * gap, book.bid)
        ask_raw = max(reservation + half_spread + level * gap, book.ask)
        bid_size = _floor_step(min(desired_size, long_cap), cfg.lot_size)
        ask_size = _floor_step(min(desired_size, short_cap), cfg.lot_size)
        if bid_size >= cfg.lot_size:
            quotes.append(Quote(level, "buy", _floor_step(bid_raw, cfg.tick_size), bid_size))
            long_cap -= bid_size
        if ask_size >= cfg.lot_size:
            quotes.append(Quote(level, "sell", _ceil_step(ask_raw, cfg.tick_size), ask_size))
            short_cap -= ask_size
    return quotes
