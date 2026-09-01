"""Deterministic multi-cycle simulator for engine/execution safety invariants."""
from __future__ import annotations

from dataclasses import dataclass

from .engine import CycleInput, EngineConfig, plan_cycle
from .execution import ActionResult, ExecutionMode, LIVE_CONFIRMATION, execute_plan
from .quote_model import Book, Inventory, QuoteConfig, RiskLimits
from .reconcile import LiveOrder, Place


@dataclass
class SimState:
    inventory: float = 0.0
    next_order_id: int = 1000
    open_orders: dict[int, LiveOrder] | None = None

    def __post_init__(self) -> None:
        if self.open_orders is None:
            self.open_orders = {}


class SimVenue:
    def __init__(self, state: SimState):
        self.state = state
        self.fail_cancel = False
        self.fail_place_indices: set[int] = set()
        self.unknown_place_indices: set[int] = set()

    def cancel_orders(self, order_ids: tuple[int, ...]) -> tuple[ActionResult, ...]:
        out = []
        for oid in order_ids:
            if self.fail_cancel:
                out.append(ActionResult(str(oid), False, "simulated cancel failure", oid, "unknown_state"))
            else:
                self.state.open_orders.pop(oid, None)
                out.append(ActionResult(str(oid), True, "success", oid, "accepted"))
        return tuple(out)

    def open_order_ids(self) -> set[int]:
        return set(self.state.open_orders)

    def place_orders(self, places: tuple[Place, ...]) -> tuple[ActionResult, ...]:
        out = []
        for index, place in enumerate(places):
            if index in self.unknown_place_indices:
                out.append(ActionResult(f"{place.side}:{place.level}", False, "missing_status", None, "unknown_state"))
                continue
            if index in self.fail_place_indices:
                out.append(ActionResult(f"{place.side}:{place.level}", False, "reject", None, "venue_reject"))
                continue
            oid = self.state.next_order_id
            self.state.next_order_id += 1
            self.state.open_orders[oid] = LiveOrder(oid, place.side, place.price, place.size, place.reduce_only, place.quote_epoch)
            out.append(ActionResult(f"{place.side}:{place.level}", True, "resting", oid, "accepted"))
        return tuple(out)

    def recover_place_results(self, places: tuple[Place, ...], results: tuple[ActionResult, ...]) -> tuple[ActionResult, ...]:
        return results


def inventory_from_net(net: float) -> Inventory:
    return Inventory(long=max(net, 0.0), short=max(-net, 0.0))


def apply_fill(state: SimState, order_id: int, fraction: float = 1.0) -> float:
    if not 0 < fraction <= 1:
        raise ValueError("fill fraction must be in (0, 1]")
    order = state.open_orders[order_id]
    filled = order.size * fraction
    state.inventory += filled if order.side == "buy" else -filled
    remaining = order.size - filled
    if remaining <= 1e-12:
        del state.open_orders[order_id]
    else:
        state.open_orders[order_id] = LiveOrder(order.order_id, order.side, order.price, remaining, order.reduce_only, order.created_at_ms)
    return filled


def run_cycle(
    state: SimState,
    venue: SimVenue,
    *,
    now_ms: int,
    book: Book,
    quote_config: QuoteConfig,
    risk_limits: RiskLimits,
    engine_config: EngineConfig = EngineConfig(),
    volatility_bps: float = 0.0,
    daily_pnl: float = 0.0,
):
    inv = inventory_from_net(state.inventory)
    cycle = CycleInput(
        now_ms=now_ms,
        book_time_ms=now_ms,
        book=book,
        venue_inventory=inv,
        ledger_inventory=inv,
        open_orders=tuple(state.open_orders.values()),
        volatility_bps=volatility_bps,
        daily_pnl=daily_pnl,
    )
    decision = plan_cycle(cycle, quote_config, risk_limits, engine_config)
    result = execute_plan(
        decision.plan,
        venue,
        mode=ExecutionMode.LIVE,
        live_enabled=True,
        confirmation=LIVE_CONFIRMATION,
        allow_opening=decision.mode.value not in {"halted", "shock"},
    )
    return decision, result
