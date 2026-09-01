"""Deterministic shadow replay helpers for evaluating adaptive maker quotes."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from .adaptive_strategy import adaptive_quote_pair
from .edge import profitability_edge
from .microstructure import depth_signal


@dataclass(frozen=True)
class ReplaySnapshot:
    time_ms: int
    levels: list[list[dict]]
    volatility_bps: float = 0.0
    directional_bps: float = 0.0
    funding_bps: float = 0.0


@dataclass(frozen=True)
class ReplayResult:
    snapshots: int
    quoted: int
    paused_bid: int
    paused_ask: int
    mean_required_edge_bps: float | None
    mean_half_spread_bps: float | None


def load_jsonl(path: str | Path) -> list[ReplaySnapshot]:
    rows: list[ReplaySnapshot] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows.append(ReplaySnapshot(
            time_ms=int(row["time_ms"]),
            levels=row["levels"],
            volatility_bps=float(row.get("volatility_bps", 0.0)),
            directional_bps=float(row.get("directional_bps", 0.0)),
            funding_bps=float(row.get("funding_bps", 0.0)),
        ))
    return rows


def replay_quotes(
    snapshots: Iterable[ReplaySnapshot], *,
    inventory: float = 0.0,
    max_inventory: float = 1.0,
    base_half_spread_bps: float = 35.0,
    fee_bps: float = 3.0,
    minimum_profit_bps: float = 8.0,
    base_size: float = 0.02,
    lot_size: float = 0.01,
) -> ReplayResult:
    quoted = paused_bid = paused_ask = 0
    required_values: list[float] = []
    half_spreads: list[float] = []
    count = 0
    for snap in snapshots:
        count += 1
        signal = depth_signal(snap.levels, depth_levels=5)
        decision = profitability_edge(
            volatility_bps=snap.volatility_bps,
            book_imbalance=signal.imbalance,
            markout_mean_bps=None,
            markout_negative_rate=None,
            directional_bps=snap.directional_bps,
            round_trip_fee_bps=fee_bps,
            minimum_profit_bps=minimum_profit_bps,
            funding_bps=snap.funding_bps,
        )
        paused_bid += int(decision.pause_bid)
        paused_ask += int(decision.pause_ask)
        required_values.append(decision.required_edge_bps)
        pair = adaptive_quote_pair(
            best_bid=signal.best_bid,
            best_ask=signal.best_ask,
            fair_value=signal.fair_value,
            inventory=inventory,
            max_inventory=max_inventory,
            volatility_bps=snap.volatility_bps,
            bid_buffer_bps=0.0,
            ask_buffer_bps=0.0,
            edge=decision,
            base_half_spread_bps=base_half_spread_bps,
            round_trip_fee_bps=fee_bps,
            minimum_profit_bps=minimum_profit_bps,
            base_size=base_size,
            lot_size=lot_size,
        )
        if not (decision.pause_bid and decision.pause_ask):
            quoted += 1
        mid = (signal.best_bid + signal.best_ask) / 2.0
        half_spreads.append(((pair.ask - pair.bid) / mid) * 5_000.0)
    return ReplayResult(
        count,
        quoted,
        paused_bid,
        paused_ask,
        sum(required_values) / len(required_values) if required_values else None,
        sum(half_spreads) / len(half_spreads) if half_spreads else None,
    )
