"""Fail-closed venue execution for Entropy reconciliation plans."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .reconcile import Place, ReconcilePlan

LIVE_CONFIRMATION = "ENTROPY_LIVE_ORDER_EXECUTION"


class ExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"


@dataclass(frozen=True)
class ActionResult:
    reference: str
    accepted: bool
    detail: str
    order_id: int | None = None
    category: str = "unknown"


@dataclass(frozen=True)
class ExecutionResult:
    mode: ExecutionMode
    status: str
    cancel_results: tuple[ActionResult, ...]
    place_results: tuple[ActionResult, ...]
    remaining_cancel_ids: tuple[int, ...] = ()
    placement_state: str = "none"


class Venue(Protocol):
    def cancel_orders(self, order_ids: tuple[int, ...]) -> tuple[ActionResult, ...]: ...
    def place_orders(self, places: tuple[Place, ...]) -> tuple[ActionResult, ...]: ...
    def open_order_ids(self) -> set[int]: ...


def _placement_state(expected: int, results: tuple[ActionResult, ...]) -> str:
    if expected == 0:
        return "none"
    accepted = sum(item.accepted for item in results)
    if accepted == expected and len(results) == expected:
        return "full"
    if accepted == 0 and len(results) == expected:
        return "zero"
    if accepted > 0:
        return "partial"
    return "unknown"


def execute_plan(
    plan: ReconcilePlan,
    venue: Venue,
    *,
    mode: ExecutionMode = ExecutionMode.DRY_RUN,
    live_enabled: bool = False,
    confirmation: str = "",
    allow_opening: bool = True,
) -> ExecutionResult:
    """Execute a plan with a verified cancel-first barrier and explicit partial-state reporting."""
    if mode is ExecutionMode.DRY_RUN:
        return ExecutionResult(mode, "planned_only", (), ())
    if not live_enabled or confirmation != LIVE_CONFIRMATION:
        return ExecutionResult(mode, "live_locked", (), ())

    cancel_ids = tuple(action.order_id for action in plan.cancels)
    cancel_results = venue.cancel_orders(cancel_ids) if cancel_ids else ()
    if len(cancel_results) != len(cancel_ids) or any(not result.accepted for result in cancel_results):
        return ExecutionResult(mode, "cancel_rejected", cancel_results, ())

    remaining = tuple(sorted(set(cancel_ids) & venue.open_order_ids())) if cancel_ids else ()
    if remaining:
        return ExecutionResult(mode, "cancel_unconfirmed", cancel_results, (), remaining)
    if not allow_opening or not plan.places:
        return ExecutionResult(mode, "cancelled_only", cancel_results, ())
    if any(not place.post_only for place in plan.places):
        return ExecutionResult(mode, "unsafe_non_post_only", cancel_results, ())

    place_results = venue.place_orders(plan.places)
    state = _placement_state(len(plan.places), place_results)
    if state != "full":
        return ExecutionResult(mode, "place_incomplete", cancel_results, place_results, placement_state=state)
    return ExecutionResult(mode, "executed", cancel_results, place_results, placement_state="full")
