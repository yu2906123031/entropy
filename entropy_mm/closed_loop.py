"""Post-execution reconciliation helpers for Entropy market making."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterable

from .execution import ExecutionMode, ExecutionResult, Venue, execute_plan
from .operations import OperationsStore, OrderSnapshot, RuntimeMetrics, WindowMetrics
from .quote_model import Inventory
from .reconcile import Cancel, ReconcilePlan


@dataclass(frozen=True)
class PositionCheck:
    matched: bool
    venue_net: float
    ledger_net: float
    difference: float


@dataclass(frozen=True)
class ClosureResult:
    execution: ExecutionResult
    order_snapshot: OrderSnapshot
    position: PositionCheck
    metrics: RuntimeMetrics
    window_metrics: WindowMetrics


def managed_ids_after_execution(store: OperationsStore, execution: ExecutionResult) -> set[int]:
    managed = store.active_managed_order_ids()
    managed -= {item.order_id for item in execution.cancel_results if item.accepted and item.order_id is not None}
    managed |= {item.order_id for item in execution.place_results if item.accepted and item.order_id is not None}
    return managed


def check_position(venue: Inventory, ledger: Inventory, *, tolerance: float = 1e-9) -> PositionCheck:
    venue_net = venue.long - venue.short
    ledger_net = ledger.long - ledger.short
    difference = venue_net - ledger_net
    return PositionCheck(abs(difference) <= tolerance, venue_net, ledger_net, difference)


def capture_orders(
    rows: Iterable[dict],
    managed_order_ids: set[int],
    *,
    coin: str,
    captured_at_ms: int | None = None,
) -> OrderSnapshot:
    order_ids: list[int] = []
    opening: list[int] = []
    reduce_only: list[int] = []
    for row in rows:
        if row.get("coin") != coin:
            continue
        oid = int(row["oid"])
        order_ids.append(oid)
        if bool(row.get("reduceOnly", False)):
            reduce_only.append(oid)
        else:
            opening.append(oid)
    orphan = sorted(set(opening) - managed_order_ids)
    return OrderSnapshot(
        captured_at_ms=time.time_ns() // 1_000_000 if captured_at_ms is None else captured_at_ms,
        order_ids=tuple(sorted(order_ids)),
        opening_order_ids=tuple(sorted(opening)),
        reduce_only_order_ids=tuple(sorted(reduce_only)),
        orphan_order_ids=tuple(orphan),
    )


def finalize_cycle(
    store: OperationsStore,
    execution: ExecutionResult,
    order_snapshot: OrderSnapshot,
    position: PositionCheck,
    *,
    planned_cancels: int,
    planned_places: int,
    synced_fills: int,
    cycle_succeeded: bool,
    latency_ms: float = 0.0,
    kept_quotes: int = 0,
    observed_quotes: int = 0,
    window_size: int = 100,
    now_ms: int | None = None,
) -> ClosureResult:
    store.record_execution(execution, created_at_ms=now_ms)
    store.record_order_snapshot(order_snapshot)
    metrics = store.update_metrics(
        {
            "cycles": 1,
            "successful_cycles": int(cycle_succeeded),
            "failed_cycles": int(not cycle_succeeded),
            "planned_cancels": planned_cancels,
            "planned_places": planned_places,
            "accepted_cancels": sum(item.accepted for item in execution.cancel_results),
            "accepted_places": sum(item.accepted for item in execution.place_results),
            "synced_fills": synced_fills,
            "orphan_observations": len(order_snapshot.orphan_order_ids),
            "position_mismatches": int(not position.matched),
        },
        now_ms=now_ms,
    )
    store.record_cycle_sample(
        succeeded=cycle_succeeded,
        latency_ms=latency_ms,
        kept_quotes=kept_quotes,
        observed_quotes=observed_quotes,
        planned_cancels=planned_cancels,
        planned_places=planned_places,
        mode=execution.mode.value,
        accepted_cancels=sum(result.accepted for result in execution.cancel_results),
        accepted_places=sum(result.accepted for result in execution.place_results),
        duration_ms=latency_ms,
        created_at_ms=now_ms,
    )
    window_metrics = store.window_metrics(last_n=window_size, mode=execution.mode.value)
    return ClosureResult(execution, order_snapshot, position, metrics, window_metrics)


def cancel_opening_orders_on_shutdown(
    venue: Venue,
    rows: Iterable[dict],
    *,
    coin: str,
    mode: ExecutionMode,
    live_enabled: bool,
    confirmation: str,
) -> ExecutionResult:
    cancels = tuple(
        Cancel(int(row["oid"]), "shutdown_opening_order")
        for row in rows
        if row.get("coin") == coin and not bool(row.get("reduceOnly", False))
    )
    return execute_plan(
        ReconcilePlan(cancels=cancels, places=(), kept_order_ids=()),
        venue,
        mode=mode,
        live_enabled=live_enabled,
        confirmation=confirmation,
        allow_opening=False,
    )
