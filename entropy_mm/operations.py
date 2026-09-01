"""Persistent post-execution state and long-run metrics for Entropy MM."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Iterable

from .execution import ExecutionResult


@dataclass(frozen=True)
class OrderSnapshot:
    captured_at_ms: int
    order_ids: tuple[int, ...]
    opening_order_ids: tuple[int, ...]
    reduce_only_order_ids: tuple[int, ...]
    orphan_order_ids: tuple[int, ...]


@dataclass(frozen=True)
class RuntimeMetrics:
    cycles: int
    successful_cycles: int
    failed_cycles: int
    planned_cancels: int
    planned_places: int
    accepted_cancels: int
    accepted_places: int
    synced_fills: int
    orphan_observations: int
    position_mismatches: int
    started_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class WindowMetrics:
    sample_count: int
    successful_cycles: int
    latency_p50_ms: float
    latency_p95_ms: float
    quote_survival_rate: float | None
    planned_order_actions: int
    accepted_order_actions: int
    planned_order_actions_per_minute: float | None
    mode: str | None


@dataclass(frozen=True)
class WindowAssessment:
    ready: bool
    alerts: tuple[str, ...]
    success_rate: float | None


def assess_window(
    metrics: WindowMetrics,
    *,
    min_samples: int = 20,
    min_success_rate: float = 0.99,
    max_p95_latency_ms: float = 5_000,
    max_planned_actions_per_minute: float = 30,
) -> WindowAssessment:
    alerts: list[str] = []
    success_rate = metrics.successful_cycles / metrics.sample_count if metrics.sample_count else None
    if metrics.sample_count < min_samples:
        alerts.append("insufficient_samples")
    if success_rate is not None and success_rate < min_success_rate:
        alerts.append("success_rate_below_threshold")
    if metrics.sample_count and metrics.latency_p95_ms > max_p95_latency_ms:
        alerts.append("p95_latency_above_threshold")
    rate = metrics.planned_order_actions_per_minute
    if rate is not None and rate > max_planned_actions_per_minute:
        alerts.append("planned_churn_above_threshold")
    if metrics.mode == "dry_run" and metrics.accepted_order_actions:
        alerts.append("dry_run_accepted_actions_detected")
    return WindowAssessment(not alerts, tuple(alerts), success_rate)


class OperationsStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_orders (
                    order_id INTEGER PRIMARY KEY,
                    created_at_ms INTEGER NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS runtime_metrics (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cycle_samples (
                    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    succeeded INTEGER NOT NULL CHECK(succeeded IN (0, 1)),
                    latency_ms REAL NOT NULL,
                    kept_quotes INTEGER NOT NULL,
                    observed_quotes INTEGER NOT NULL,
                    planned_cancels INTEGER NOT NULL,
                    planned_places INTEGER NOT NULL
                );
                """
            )
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(cycle_samples)")}
            migrations = {
                "mode": "TEXT NOT NULL DEFAULT 'dry_run'",
                "accepted_cancels": "INTEGER NOT NULL DEFAULT 0",
                "accepted_places": "INTEGER NOT NULL DEFAULT 0",
                "duration_ms": "REAL NOT NULL DEFAULT 0",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE cycle_samples ADD COLUMN {name} {declaration}")
            db.execute("PRAGMA user_version=1")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000

    def record_execution(self, result: ExecutionResult, *, created_at_ms: int | None = None) -> None:
        now = self._now_ms() if created_at_ms is None else created_at_ms
        payload = json.dumps(asdict(result), default=lambda value: value.value, sort_keys=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO execution_runs(created_at_ms, mode, status, payload) VALUES (?, ?, ?, ?)",
                (now, result.mode.value, result.status, payload),
            )
            for item in result.cancel_results:
                if item.accepted and item.order_id is not None:
                    db.execute("UPDATE managed_orders SET active=0 WHERE order_id=?", (item.order_id,))
            for item in result.place_results:
                if item.accepted and item.order_id is not None:
                    db.execute(
                        "INSERT INTO managed_orders(order_id, created_at_ms, active) VALUES (?, ?, 1) "
                        "ON CONFLICT(order_id) DO UPDATE SET active=1",
                        (item.order_id, now),
                    )

    def active_managed_order_ids(self) -> set[int]:
        with self._connect() as db:
            return {int(row["order_id"]) for row in db.execute("SELECT order_id FROM managed_orders WHERE active=1")}

    def record_order_snapshot(self, snapshot: OrderSnapshot) -> None:
        payload = json.dumps(asdict(snapshot), sort_keys=True)
        active = set(snapshot.order_ids)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO order_snapshots(created_at_ms, payload) VALUES (?, ?)",
                (snapshot.captured_at_ms, payload),
            )
            if active:
                marks = ",".join("?" for _ in active)
                db.execute(f"UPDATE managed_orders SET active=0 WHERE active=1 AND order_id NOT IN ({marks})", tuple(active))
            else:
                db.execute("UPDATE managed_orders SET active=0 WHERE active=1")

    def update_metrics(self, increments: dict[str, int], *, now_ms: int | None = None) -> RuntimeMetrics:
        allowed = {
            "cycles", "successful_cycles", "failed_cycles", "planned_cancels", "planned_places",
            "accepted_cancels", "accepted_places", "synced_fills", "orphan_observations", "position_mismatches",
        }
        unknown = set(increments) - allowed
        if unknown:
            raise ValueError(f"unknown metric keys: {sorted(unknown)}")
        now = self._now_ms() if now_ms is None else now_ms
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runtime_metrics WHERE key='started_at_ms'").fetchone() is None:
                db.execute("INSERT INTO runtime_metrics VALUES ('started_at_ms', ?)", (now,))
            for key, amount in increments.items():
                db.execute(
                    "INSERT INTO runtime_metrics(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                    (key, int(amount)),
                )
            db.execute(
                "INSERT INTO runtime_metrics(key, value) VALUES ('updated_at_ms', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now,),
            )
        return self.metrics()

    def metrics(self) -> RuntimeMetrics:
        with self._connect() as db:
            values = {str(row["key"]): int(row["value"]) for row in db.execute("SELECT key, value FROM runtime_metrics")}
        fields = RuntimeMetrics.__dataclass_fields__
        return RuntimeMetrics(**{name: values.get(name, 0) for name in fields})

    def execution_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0])

    def record_cycle_sample(
        self,
        *,
        succeeded: bool,
        latency_ms: float,
        kept_quotes: int,
        observed_quotes: int,
        planned_cancels: int,
        planned_places: int,
        mode: str = "dry_run",
        accepted_cancels: int = 0,
        accepted_places: int = 0,
        duration_ms: float | None = None,
        created_at_ms: int | None = None,
    ) -> None:
        duration = latency_ms if duration_ms is None else duration_ms
        if latency_ms < 0 or duration < 0 or min(
            kept_quotes, observed_quotes, planned_cancels, planned_places,
            accepted_cancels, accepted_places,
        ) < 0:
            raise ValueError("cycle sample values must be non-negative")
        if kept_quotes > observed_quotes:
            raise ValueError("kept quotes cannot exceed observed quotes")
        now = self._now_ms() if created_at_ms is None else created_at_ms
        with self._connect() as db:
            db.execute(
                "INSERT INTO cycle_samples(created_at_ms, succeeded, latency_ms, kept_quotes, observed_quotes, "
                "planned_cancels, planned_places, mode, accepted_cancels, accepted_places, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, int(succeeded), latency_ms, kept_quotes, observed_quotes, planned_cancels,
                 planned_places, mode, accepted_cancels, accepted_places, duration),
            )

    def window_metrics(self, *, last_n: int = 100, mode: str | None = None) -> WindowMetrics:
        if last_n < 1:
            raise ValueError("last_n must be positive")
        with self._connect() as db:
            if mode is None:
                rows = list(db.execute("SELECT * FROM cycle_samples ORDER BY sample_id DESC LIMIT ?", (last_n,)))
            else:
                rows = list(db.execute(
                    "SELECT * FROM cycle_samples WHERE mode=? ORDER BY sample_id DESC LIMIT ?", (mode, last_n)
                ))
        if not rows:
            return WindowMetrics(0, 0, 0.0, 0.0, None, 0, 0, None, mode)
        rows.reverse()
        latencies = sorted(float(row["latency_ms"]) for row in rows)

        def percentile(fraction: float) -> float:
            import math
            index = max(0, min(len(latencies) - 1, math.ceil(fraction * len(latencies)) - 1))
            return latencies[index]

        kept = sum(int(row["kept_quotes"]) for row in rows)
        observed = sum(int(row["observed_quotes"]) for row in rows)
        planned_actions = sum(int(row["planned_cancels"]) + int(row["planned_places"]) for row in rows)
        accepted_actions = sum(int(row["accepted_cancels"]) + int(row["accepted_places"]) for row in rows)
        started_at_ms = min(int(row["created_at_ms"]) - float(row["duration_ms"]) for row in rows)
        span_minutes = (int(rows[-1]["created_at_ms"]) - started_at_ms) / 60_000
        return WindowMetrics(
            sample_count=len(rows),
            successful_cycles=sum(int(row["succeeded"]) for row in rows),
            latency_p50_ms=percentile(0.50),
            latency_p95_ms=percentile(0.95),
            quote_survival_rate=kept / observed if observed else None,
            planned_order_actions=planned_actions,
            accepted_order_actions=accepted_actions,
            planned_order_actions_per_minute=planned_actions / span_minutes if span_minutes > 0 else None,
            mode=mode,
        )
