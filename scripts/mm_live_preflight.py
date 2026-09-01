#!/usr/bin/env python3
"""Fail-closed read-only gate report for a future minimum-size Entropy live probe."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hyperliquid.info import Info
from hyperliquid.utils import constants

from entropy_mm.closed_loop import check_position
from entropy_mm.ledger import LotLedger
from entropy_mm.quote_model import Inventory

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
ABSOLUTE_PROBE_CAP_USD = 20.0
MIN_PROBE_NOTIONAL_USD = 10.0
LOCK_MESSAGE = "daemon live execution remains locked; use read-only mode"


def inventory_for(state: dict, coin: str) -> Inventory:
    for row in state.get("assetPositions", []):
        position = row.get("position", {})
        if position.get("coin") == coin:
            size = float(position.get("szi", 0) or 0)
            return Inventory(long=max(size, 0), short=max(-size, 0))
    return Inventory()


def daemon_live_lock_check() -> tuple[bool, dict[str, object]]:
    env = dict(os.environ)
    env["ENTROPY_LIVE_ENABLED"] = "true"
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/mm_daemon.py"), "--max-cycles", "1"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, {"error": type(exc).__name__}
    output = f"{result.stdout}\n{result.stderr}"
    passed = result.returncode != 0 and LOCK_MESSAGE in output
    return passed, {"returncode": result.returncode, "lock_message_observed": LOCK_MESSAGE in output}


def private_key_environment_clear() -> tuple[bool, list[str]]:
    watched = {"ENTROPY_PRIVATE_KEY", "HYPERLIQUID_PRIVATE_KEY", "HL_PRIVATE_KEY", "PRIVATE_KEY"}
    sensitive_names = sorted(name for name, value in os.environ.items() if value and name.upper() in watched)
    return not sensitive_names, sensitive_names


def build_report() -> dict[str, object]:
    account = os.environ.get("ENTROPY_ACCOUNT", "")
    operator = os.environ.get("ENTROPY_OPERATOR_ADDRESS", "")
    coin = os.environ.get("ENTROPY_COIN", "ZEC")
    ledger_path = os.environ.get("ENTROPY_LEDGER_PATH", "runtime/entropy_zec_mm.sqlite3")
    try:
        configured_max = float(os.environ.get("ENTROPY_LIVE_PROBE_MAX_USD", "20"))
        order_size = float(os.environ.get("ENTROPY_ORDER_SIZE", "0.02"))
    except ValueError:
        configured_max = math.nan
        order_size = math.nan

    daemon_locked, daemon_details = daemon_live_lock_check()
    key_env_clear, exposed_key_names = private_key_environment_clear()
    checks: dict[str, bool] = {
        "account_address_valid": bool(ADDRESS.fullmatch(account)),
        "operator_address_valid": bool(ADDRESS.fullmatch(operator)),
        "operator_nonzero": bool(ADDRESS.fullmatch(operator)) and int(operator, 16) != 0,
        "separate_operator_wallet": bool(account and operator and account.lower() != operator.lower()),
        "live_daemon_lock_runtime_verified": daemon_locked,
        "private_key_environment_clear": key_env_clear,
        "probe_cap_valid": math.isfinite(configured_max) and MIN_PROBE_NOTIONAL_USD <= configured_max <= ABSOLUTE_PROBE_CAP_USD,
        "order_size_valid": math.isfinite(order_size) and order_size > 0,
        "network_checks_completed": False,
    }
    details: dict[str, object] = {
        "account": account,
        "operator": operator,
        "coin": coin,
        "absolute_probe_cap_usd": ABSOLUTE_PROBE_CAP_USD,
        "configured_probe_cap_usd": configured_max if math.isfinite(configured_max) else None,
        "daemon_lock_probe": daemon_details,
        "exposed_private_key_environment_names": exposed_key_names,
    }

    if checks["account_address_valid"]:
        try:
            info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
            state = info.user_state(account)
            spot_state = info.spot_user_state(account)
            orders = [row for row in info.open_orders(account) if row.get("coin") == coin]
            mid = float(info.all_mids()[coin])
            venue_inventory = inventory_for(state, coin)
            ledger_inventory = LotLedger(ledger_path).snapshot().inventory
            position = check_position(venue_inventory, ledger_inventory)
            perp_equity = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
            spot_usdc = sum(
                float(row.get("total", 0) or 0)
                for row in spot_state.get("balances", [])
                if row.get("coin") == "USDC"
            )
            opening = [int(row["oid"]) for row in orders if not bool(row.get("reduceOnly", False))]
            probe_notional = order_size * mid
            effective_cap = min(configured_max, ABSOLUTE_PROBE_CAP_USD) if math.isfinite(configured_max) else math.nan
            checks.update(
                {
                    "network_checks_completed": True,
                    "positive_perp_equity": math.isfinite(perp_equity) and perp_equity > 0,
                    "flat_venue_position": abs(position.venue_net) <= 1e-9,
                    "ledger_position_matched": position.matched,
                    "zero_opening_orders": not opening,
                    "probe_notional_bounded": (
                        math.isfinite(probe_notional) and math.isfinite(effective_cap)
                        and MIN_PROBE_NOTIONAL_USD <= probe_notional <= effective_cap
                    ),
                }
            )
            details.update(
                {
                    "perp_equity": perp_equity,
                    "spot_usdc": spot_usdc,
                    "total_account_value": perp_equity + spot_usdc,
                    "mid": mid,
                    "order_size": order_size,
                    "probe_notional_usd": probe_notional,
                    "effective_probe_cap_usd": effective_cap,
                    "opening_order_ids": opening,
                    "position": asdict(position),
                }
            )
        except Exception as exc:
            details["network_error"] = type(exc).__name__

    failed = sorted(key for key, value in checks.items() if not value)
    return {
        "stage": "read_only_live_preflight",
        "ready_for_explicit_write_probe_approval": not failed,
        "checks": checks,
        "failed_checks": failed,
        "details": details,
        "signed_calls": 0,
        "signed_client_constructed": False,
        "daemon_live_path": "runtime_verified_locked" if daemon_locked else "lock_unverified",
    }


def main() -> None:
    report = build_report()
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["ready_for_explicit_write_probe_approval"] else 2)


if __name__ == "__main__":
    main()
