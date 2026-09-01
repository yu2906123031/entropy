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


@dataclass(frozen=True)
class ExecutionResult:
    mode: ExecutionMode
    status: str
    cancel_results: tuple[ActionResult, ...]
    place_results: tuple[ActionResult, ...]
    remaining_cancel_ids: tuple[int, ...] = ()


class Venue(Protocol):
    def cancel_orders(self, order_ids: tuple[int, ...]) -> tuple[ActionResult, ...]: ...
    def place_orders(self, places: tuple[Place, ...]) -> tuple[ActionResult, ...]: ...
    def open_order_ids(self) -> set[int]: ...


def execute_plan(
    plan: ReconcilePlan,
    venue: Venue,
    *,
    mode: ExecutionMode = ExecutionMode.DRY_RUN,
    live_enabled: bool = False,
    confirmation: str = "",
    allow_opening: bool = True,
) -> ExecutionResult:
    """Execute a plan with a verified cancel-first barrier.

    Dry-run performs no Venue calls. Live execution requires two independent
    gates. Opening orders are sent only after every requested cancellation is
    accepted and absent from a fresh open-order snapshot.
    """
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
    if len(place_results) != len(plan.places) or any(not result.accepted for result in place_results):
        return ExecutionResult(mode, "place_incomplete", cancel_results, place_results)
    return ExecutionResult(mode, "executed", cancel_results, place_results)
