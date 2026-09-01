"""Persistent maker quote exposure and empirical fill-probability learning."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import sqlite3


@dataclass(frozen=True)
class Exposure:
    order_id: int
    side: str
    price: float
    size: float
    created_at_ms: int
    distance_bps: float
    queue_ahead: float


@dataclass(frozen=True)
class FillQuality:
    samples: int
    fills: int
    fill_rate: float | None
    mean_exposure_ms: float | None
    mean_distance_bps: float | None
    mean_queue_ahead_ratio: float | None


def quote_key(order_id: int) -> str:
    return str(int(order_id))


class QuoteQualityStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS quote_exposures ("
                "order_id INTEGER PRIMARY KEY, side TEXT NOT NULL, price REAL NOT NULL, size REAL NOT NULL, "
                "created_at_ms INTEGER NOT NULL, distance_bps REAL NOT NULL, queue_ahead REAL NOT NULL, "
                "closed_at_ms INTEGER, outcome TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def open(self, exposure: Exposure) -> None:
        if exposure.side not in {"buy", "sell"} or exposure.price <= 0 or exposure.size <= 0:
            raise ValueError("invalid quote exposure")
        if exposure.created_at_ms < 0 or exposure.distance_bps < 0 or exposure.queue_ahead < 0:
            raise ValueError("invalid quote exposure metrics")
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO quote_exposures(order_id, side, price, size, created_at_ms, distance_bps, queue_ahead) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    exposure.order_id,
                    exposure.side,
                    exposure.price,
                    exposure.size,
                    exposure.created_at_ms,
                    exposure.distance_bps,
                    exposure.queue_ahead,
                ),
            )

    def close(self, order_id: int, *, closed_at_ms: int, filled: bool) -> bool:
        if closed_at_ms < 0:
            raise ValueError("closed_at_ms must be non-negative")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE quote_exposures SET closed_at_ms=?, outcome=? WHERE order_id=? AND closed_at_ms IS NULL",
                (closed_at_ms, "fill" if filled else "cancel", int(order_id)),
            )
        return cursor.rowcount > 0

    def open_ids(self) -> set[int]:
        with self._connect() as db:
            rows = db.execute("SELECT order_id FROM quote_exposures WHERE closed_at_ms IS NULL").fetchall()
        return {int(row[0]) for row in rows}

    def quality(self, *, side: str | None = None, last_n: int = 200) -> FillQuality:
        if side not in {None, "buy", "sell"} or last_n < 1:
            raise ValueError("invalid quality query")
        query = (
            "SELECT side, size, created_at_ms, closed_at_ms, outcome, distance_bps, queue_ahead "
            "FROM quote_exposures WHERE closed_at_ms IS NOT NULL"
        )
        params: list[object] = []
        if side is not None:
            query += " AND side=?"
            params.append(side)
        query += " ORDER BY closed_at_ms DESC LIMIT ?"
        params.append(last_n)
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        if not rows:
            return FillQuality(0, 0, None, None, None, None)
        fills = sum(row[4] == "fill" for row in rows)
        exposure_ms = [max(0, int(row[3]) - int(row[2])) for row in rows]
        distances = [float(row[5]) for row in rows]
        queue_ratios = [float(row[6]) / max(float(row[1]), 1e-12) for row in rows]
        return FillQuality(
            len(rows),
            fills,
            fills / len(rows),
            sum(exposure_ms) / len(exposure_ms),
            sum(distances) / len(distances),
            sum(queue_ratios) / len(queue_ratios),
        )


def empirical_fill_multiplier(quality: FillQuality, *, min_samples: int = 20) -> float:
    """Conservative size multiplier learned from actual maker quote outcomes."""
    if quality.samples < min_samples or quality.fill_rate is None:
        return 1.0
    rate_component = min(1.0, max(0.25, quality.fill_rate / 0.25))
    queue_component = 1.0
    if quality.mean_queue_ahead_ratio is not None and math.isfinite(quality.mean_queue_ahead_ratio):
        queue_component = 1.0 / (1.0 + max(0.0, quality.mean_queue_ahead_ratio) / 20.0)
        queue_component = max(0.35, min(1.0, queue_component))
    return max(0.20, min(1.0, (rate_component * queue_component) ** 0.5))
