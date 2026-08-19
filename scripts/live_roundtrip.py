"""Place one distant HYPE ALO order with Entropy attribution, then cancel it."""
import getpass
import json
import math
import sys
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

ACCOUNT = "0x78605485604BA45ce0eF860DB1594ec810154477"
EXPECTED_AGENT = "0x3B4347B99BB749eBdD6DE720736796E1b7Dfe4a6"
BUILDER = "0xcd254d2a328f7f67c7c6fef930a4757516f7b601"
KEYSTORE = Path(__file__).resolve().parents[1] / "secrets" / "agent-keystore.json"
CONFIRMATION = "PLACE AND CANCEL"

def main():
    if "--execute" not in sys.argv:
        raise SystemExit("Safety lock: rerun with --execute to enable the confirmation prompt")
    if not KEYSTORE.exists():
        raise SystemExit(f"Missing encrypted Agent keystore: {KEYSTORE}")

    password = getpass.getpass("Agent keystore password (hidden): ")
    try:
        encrypted = json.loads(KEYSTORE.read_text(encoding="utf-8"))
        private_key = Account.decrypt(encrypted, password)
        agent = Account.from_key(private_key)
    except Exception as exc:
        raise SystemExit("Could not decrypt Agent keystore") from exc
    if agent.address.lower() != EXPECTED_AGENT.lower():
        raise SystemExit(f"Keystore Agent mismatch: {agent.address}")

    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    role = info.user_role(agent.address)
    if role.get("role") != "agent" or role.get("data", {}).get("user", "").lower() != ACCOUNT.lower():
        raise SystemExit(f"Agent is not authorized for expected account: {role}")

    spot = info.spot_user_state(ACCOUNT)
    usdc = next((float(x["total"]) - float(x["hold"]) for x in spot.get("balances", []) if x["coin"] == "USDC"), 0.0)
    if usdc < 5:
        raise SystemExit(f"Insufficient available USDC for test: {usdc}")

    book = info.l2_snapshot("HYPE")
    best_bid = float(book["levels"][0][0]["px"])
    price = float(f"{best_bid * 0.80:.4g}")
    size = math.ceil((10.50 / price) * 100) / 100
    notional = price * size
    cloid = Cloid.from_int(int.from_bytes(__import__("secrets").token_bytes(16), "big"))
    builder = {"b": BUILDER, "f": 0}

    preview = {
        "account": ACCOUNT,
        "agent": agent.address,
        "coin": "HYPE",
        "side": "buy",
        "type": "Post Only (ALO)",
        "bestBid": best_bid,
        "limitPrice": price,
        "distanceBelowBidPct": round((1 - price / best_bid) * 100, 2),
        "size": size,
        "notionalUsd": round(notional, 4),
        "builder": builder,
        "action": "place, verify resting, immediately cancel",
    }
    print(json.dumps(preview, indent=2))
    typed = input(f'Type exactly "{CONFIRMATION}" to broadcast: ')
    if typed != CONFIRMATION:
        raise SystemExit("Cancelled locally; nothing was broadcast")

    exchange = Exchange(agent, constants.MAINNET_API_URL, account_address=ACCOUNT, timeout=30)
    result = exchange.order("HYPE", True, size, price, {"limit": {"tif": "Alo"}}, False, cloid, builder)
    print("PLACE RESULT")
    print(json.dumps(result, indent=2))

    status = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
    resting = status.get("resting") if isinstance(status, dict) else None
    if not resting:
        raise SystemExit(f"Order did not rest; no cancel sent. Status: {status}")
    oid = resting["oid"]

    cancel = exchange.cancel("HYPE", oid)
    print("CANCEL RESULT")
    print(json.dumps(cancel, indent=2))
    remaining = [x for x in info.open_orders(ACCOUNT) if x.get("oid") == oid]
    if remaining:
        raise SystemExit(f"WARNING: order {oid} still appears open; cancel it manually")
    print(f"SUCCESS: order {oid} rested and was cancelled; no matching open order remains.")

if __name__ == "__main__":
    main()
