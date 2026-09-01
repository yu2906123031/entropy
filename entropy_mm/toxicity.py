"""Maker-fill markout analytics used to detect adverse selection."""
from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class FillObservation:
    trade_id: str
    side: str
    price: float
    size: float
    time_ms: int


@dataclass(frozen=True)
class Markout:
    trade_id: str
    horizon_ms: int
    mark_price: float
    markout_bps: float


@dataclass(frozen=True)
class ToxicitySummary:
    count: int
    mean_markout_bps: float | None
    negative_rate: float | None
    toxic: bool


def signed_markout_bps(side: str, fill_price: float, mark_price: float) -> float:
    """Positive means price moved in the maker fill's favor after execution."""
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if not all(math.isfinite(v) and v > 0 for v in (fill_price, mark_price)):
        raise ValueError("prices must be finite and positive")
    raw = (mark_price / fill_price - 1.0) * 10_000
    return raw if side == "buy" else -raw


@dataclass
class MarkoutTracker:
    horizons_ms: tuple[int, ...] = (1_000, 5_000, 30_000)
    pending: dict[str, FillObservation] = field(default_factory=dict)
    completed: list[Markout] = field(default_factory=list)
    _done: set[tuple[str, int]] = field(default_factory=set)

    def add_fill(self, fill: FillObservation) -> None:
        if fill.side not in {"buy", "sell"} or fill.price <= 0 or fill.size <= 0:
            raise ValueError("invalid fill observation")
        self.pending.setdefault(fill.trade_id, fill)

    def observe(self, now_ms: int, mark_price: float) -> tuple[Markout, ...]:
        produced: list[Markout] = []
        for trade_id, fill in list(self.pending.items()):
            age = now_ms - fill.time_ms
            if age < 0:
                continue
            for horizon in self.horizons_ms:
                key = (trade_id, horizon)
                if key not in self._done and age >= horizon:
                    item = Markout(trade_id, horizon, mark_price, signed_markout_bps(fill.side, fill.price, mark_price))
                    self.completed.append(item)
                    self._done.add(key)
                    produced.append(item)
            if all((trade_id, horizon) in self._done for horizon in self.horizons_ms):
                self.pending.pop(trade_id, None)
        return tuple(produced)

    def summary(self, horizon_ms: int = 5_000, *, toxic_mean_bps: float = -2.0, min_samples: int = 10) -> ToxicitySummary:
        values = [m.markout_bps for m in self.completed if m.horizon_ms == horizon_ms]
        if not values:
            return ToxicitySummary(0, None, None, False)
        mean = sum(values) / len(values)
        negative_rate = sum(v < 0 for v in values) / len(values)
        return ToxicitySummary(len(values), mean, negative_rate, len(values) >= min_samples and mean <= toxic_mean_bps)
