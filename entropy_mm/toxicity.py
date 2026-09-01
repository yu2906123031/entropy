"""Maker-fill markout analytics used to detect adverse selection."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import sqlite3


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
        if fill.side not in {"buy", "sell"} or fill.price <= 0 or fill.size <= 0 or fill.time_ms < 0:
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
        return summarize_markouts(values, toxic_mean_bps=toxic_mean_bps, min_samples=min_samples)


def summarize_markouts(values: list[float], *, toxic_mean_bps: float = -2.0, min_samples: int = 10) -> ToxicitySummary:
    if not values:
        return ToxicitySummary(0, None, None, False)
    mean = sum(values) / len(values)
    negative_rate = sum(value < 0 for value in values) / len(values)
    return ToxicitySummary(len(values), mean, negative_rate, len(values) >= min_samples and mean <= toxic_mean_bps)


class MarkoutStore:
    """Small SQLite store for restart-safe rolling markout diagnostics."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS markouts ("
                "trade_id TEXT NOT NULL, horizon_ms INTEGER NOT NULL, mark_price REAL NOT NULL, "
                "markout_bps REAL NOT NULL, recorded_at_ms INTEGER NOT NULL, "
                "PRIMARY KEY(trade_id, horizon_ms))"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def record(self, markouts: tuple[Markout, ...] | list[Markout], *, recorded_at_ms: int) -> int:
        if recorded_at_ms < 0:
            raise ValueError("recorded_at_ms must be non-negative")
        inserted = 0
        with self._connect() as db:
            for item in markouts:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO markouts(trade_id, horizon_ms, mark_price, markout_bps, recorded_at_ms) VALUES (?, ?, ?, ?, ?)",
                    (item.trade_id, item.horizon_ms, item.mark_price, item.markout_bps, recorded_at_ms),
                )
                inserted += int(cursor.rowcount > 0)
        return inserted

    def summary(
        self,
        horizon_ms: int = 5_000,
        *,
        last_n: int = 100,
        toxic_mean_bps: float = -2.0,
        min_samples: int = 10,
    ) -> ToxicitySummary:
        if last_n < 1:
            raise ValueError("last_n must be positive")
        with self._connect() as db:
            rows = db.execute(
                "SELECT markout_bps FROM markouts WHERE horizon_ms=? ORDER BY recorded_at_ms DESC LIMIT ?",
                (horizon_ms, last_n),
            ).fetchall()
        values = [float(row[0]) for row in rows]
        return summarize_markouts(values, toxic_mean_bps=toxic_mean_bps, min_samples=min_samples)
