#!/usr/bin/env python3
"""Plan Entropy opening-order cleanup during read-only daemon shutdown."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hyperliquid.info import Info
from hyperliquid.utils import constants

from entropy_mm.closed_loop import cancel_opening_orders_on_shutdown, capture_orders
from entropy_mm.execution import ExecutionMode
from entropy_mm.operations import OperationsStore

ACCOUNT = os.environ.get("ENTROPY_ACCOUNT", "0x78605485604BA45ce0eF860DB1594ec810154477")
COIN = os.environ.get("ENTROPY_COIN", "ZEC")
OPERATIONS_PATH = os.environ.get("ENTROPY_OPERATIONS_PATH", "runtime/entropy_mm_operations.sqlite3")


class NoCallVenue:
    def cancel_orders(self, order_ids):
        raise AssertionError("shutdown dry-run attempted cancellation")

    def place_orders(self, places):
        raise AssertionError("shutdown dry-run attempted placement")

    def open_order_ids(self):
        raise AssertionError("shutdown dry-run attempted order query through execution venue")


def main() -> None:
    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    rows = info.open_orders(ACCOUNT)
    store = OperationsStore(OPERATIONS_PATH)
    snapshot = capture_orders(rows, store.active_managed_order_ids(), coin=COIN)
    result = cancel_opening_orders_on_shutdown(
        NoCallVenue(),
        rows,
        coin=COIN,
        mode=ExecutionMode.DRY_RUN,
        live_enabled=False,
        confirmation="",
    )
    print(json.dumps({
        "mode": "read_only_dry_run",
        "opening_order_ids": list(snapshot.opening_order_ids),
        "preserved_reduce_only_order_ids": list(snapshot.reduce_only_order_ids),
        "execution": asdict(result),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
