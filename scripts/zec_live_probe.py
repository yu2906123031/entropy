#!/usr/bin/env python3
"""Place and cancel one bounded ZEC post-only probe order."""
from __future__ import annotations

import getpass
import json
import os
import time
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

ACCOUNT = "0x78605485604BA45ce0eF860DB1594ec810154477"
AGENT = "0x3B4347B99BB749eBdD6DE720736796E1b7Dfe4a6"
COIN = "ZEC"
SIZE = 0.02
MAX_NOTIONAL = 20.0
KEYSTORE = Path(__file__).resolve().parents[1] / "secrets" / "agent-keystore.json"


def password() -> str:
    path = os.environ.get("ENTROPY_CREDENTIAL_FILE")
    return Path(path).read_text().rstrip("\r\n") if path else getpass.getpass("Keystore password: ")


def main() -> None:
    key = Account.decrypt(json.loads(KEYSTORE.read_text()), password())
    agent = Account.from_key(key)
    if agent.address.lower() != AGENT.lower():
        raise SystemExit("agent address mismatch")
    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    role = info.user_role(agent.address)
    if role.get("role") != "agent" or role.get("data", {}).get("user", "").lower() != ACCOUNT.lower():
        raise SystemExit("agent authorization mismatch")
    if any(o.get("coin") == COIN for o in info.open_orders(ACCOUNT)):
        raise SystemExit("existing ZEC orders present")
    if any(p.get("position", {}).get("coin") == COIN and float(p["position"].get("szi", 0)) for p in info.user_state(ACCOUNT).get("assetPositions", [])):
        raise SystemExit("existing ZEC position present")

    book = info.l2_snapshot(COIN)
    bid = float(book["levels"][0][0]["px"])
    price = round(bid - 0.1, 1)
    notional = price * SIZE
    if not (10.0 <= notional <= MAX_NOTIONAL):
        raise SystemExit(f"probe notional outside bounds: {notional}")

    exchange = Exchange(agent, constants.MAINNET_API_URL, account_address=ACCOUNT, timeout=30)
    oid = None
    try:
        result = exchange.order(COIN, True, SIZE, price, {"limit": {"tif": "Alo"}}, False)
        status = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
        if not isinstance(status, dict) or "resting" not in status:
            raise RuntimeError(f"probe rejected: {status}")
        oid = int(status["resting"]["oid"])
        print(json.dumps({"accepted": True, "coin": COIN, "side": "buy", "size": SIZE, "price": price, "notional": notional, "oid": oid}))
        time.sleep(3)
    finally:
        if oid is not None:
            cancel = exchange.cancel(COIN, oid)
            print(json.dumps({"cancel_requested": True, "oid": oid, "result": cancel}))
            for _ in range(10):
                if not any(int(o["oid"]) == oid for o in info.open_orders(ACCOUNT)):
                    print(json.dumps({"cancel_verified": True, "oid": oid}))
                    break
                time.sleep(1)
            else:
                raise RuntimeError("probe order still open after cancel")


if __name__ == "__main__":
    main()
