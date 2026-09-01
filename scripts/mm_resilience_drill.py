#!/usr/bin/env python3
"""Deterministic timeout, partial-execution and restart-recovery drills."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from entropy_mm.execution import ActionResult, ExecutionMode, LIVE_CONFIRMATION, execute_plan
from entropy_mm.lifecycle import LoopConfig, run_loop
from entropy_mm.operations import OperationsStore
from entropy_mm.reconcile import Place, ReconcilePlan


class PartialVenue:
    def __init__(self):
        self.place_calls = 0

    def cancel_orders(self, order_ids):
        return ()

    def open_order_ids(self):
        return set()

    def place_orders(self, places):
        self.place_calls += 1
        return (ActionResult("buy:0", True, "resting", 7001),)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="entropy-drill-") as folder:
        root = Path(folder)
        attempts = 0

        def timeout_cycle():
            nonlocal attempts
            attempts += 1
            raise TimeoutError("simulated API deadline")

        timeout_health = run_loop(
            timeout_cycle,
            lambda: None,
            health_path=root / "timeout_health.json",
            config=LoopConfig(interval_seconds=0, initial_backoff_seconds=0, max_consecutive_failures=2),
            sleep=lambda _: None,
        )

        plan = ReconcilePlan(
            cancels=(),
            places=(
                Place(level=0, side="buy", price=10.0, size=1.0, post_only=True),
                Place(level=0, side="sell", price=11.0, size=1.0, post_only=True),
            ),
            kept_order_ids=(),
        )
        venue = PartialVenue()
        partial = execute_plan(
            plan,
            venue,
            mode=ExecutionMode.LIVE,
            live_enabled=True,
            confirmation=LIVE_CONFIRMATION,
        )

        db_path = root / "ops.sqlite3"
        first = OperationsStore(db_path)
        first.record_execution(partial, created_at_ms=1_000)
        first.update_metrics({"cycles": 1, "failed_cycles": 1}, now_ms=1_000)
        recovered = OperationsStore(db_path)
        restart = {
            "execution_count": recovered.execution_count(),
            "active_managed_order_ids": sorted(recovered.active_managed_order_ids()),
            "cycles": recovered.metrics().cycles,
            "failed_cycles": recovered.metrics().failed_cycles,
        }

        report = {
            "timeout": {
                "attempts": attempts,
                "consecutive_failures": timeout_health.consecutive_failures,
                "stop_reason": timeout_health.stop_reason,
                "last_error": timeout_health.last_error,
            },
            "partial_execution": asdict(partial),
            "restart_recovery": restart,
            "passed": (
                timeout_health.stop_reason == "failure_limit"
                and partial.status == "place_incomplete"
                and restart["execution_count"] == 1
                and restart["active_managed_order_ids"] == [7001]
            ),
            "external_signed_calls": 0,
        }
        print(json.dumps(report, indent=2, sort_keys=True, default=lambda value: value.value))
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
