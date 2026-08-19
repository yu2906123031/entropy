"""Open and immediately close the minimum HYPE position with Entropy attribution."""
import getpass
import json
import math
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

ACCOUNT = "0x78605485604BA45ce0eF860DB1594ec810154477"
EXPECTED_AGENT = "0x3B4347B99BB749eBdD6DE720736796E1b7Dfe4a6"
BUILDER = {"b": "0xcd254d2a328f7f67c7c6fef930a4757516f7b601", "f": 0}
KEYSTORE = Path(__file__).resolve().parents[1] / "secrets" / "agent-keystore.json"

def hype_position(state):
    for item in state.get("assetPositions", []):
        position = item.get("position", {})
        if position.get("coin") == "HYPE" and float(position.get("szi", 0) or 0) != 0:
            return position
    return None

def main():
    password = getpass.getpass("Agent keystore password (hidden): ")
    try:
        key = Account.decrypt(json.loads(KEYSTORE.read_text(encoding="utf-8")), password)
        agent = Account.from_key(key)
    except Exception as exc:
        raise SystemExit("Could not decrypt Agent keystore") from exc
    if agent.address.lower() != EXPECTED_AGENT.lower():
        raise SystemExit(f"Unexpected Agent: {agent.address}")

    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    role = info.user_role(agent.address)
    if role.get("role") != "agent" or role.get("data", {}).get("user", "").lower() != ACCOUNT.lower():
        raise SystemExit(f"Agent authorization mismatch: {role}")
    if hype_position(info.user_state(ACCOUNT)):
        raise SystemExit("Refusing to start: account already has a HYPE position")

    spot = info.spot_user_state(ACCOUNT)
    available = next((float(x["total"]) - float(x["hold"]) for x in spot.get("balances", []) if x["coin"] == "USDC"), 0.0)
    if available < 7.5:
        raise SystemExit(f"Available USDC fell below safety threshold: {available}")

    mid = float(info.all_mids()["HYPE"])
    size = math.ceil((10.25 / mid) * 100) / 100
    print(json.dumps({"availableUsdc": available, "coin": "HYPE", "open": "market buy", "close": "immediate reduce-only market sell", "size": size, "estimatedNotionalUsd": round(size * mid, 4), "leverage": "2x cross", "maxSlippageEachLeg": "1%", "builder": BUILDER}, indent=2))
    if input('Type exactly "EXECUTE $8 TEST": ') != "EXECUTE $8 TEST":
        raise SystemExit("Cancelled locally; nothing was broadcast")

    exchange = Exchange(agent, constants.MAINNET_API_URL, account_address=ACCOUNT, timeout=30)
    leverage = exchange.update_leverage(2, "HYPE", True)
    print("LEVERAGE RESULT", json.dumps(leverage))
    opened = exchange.market_open("HYPE", True, size, None, 0.01, builder=BUILDER)
    print("OPEN RESULT", json.dumps(opened, indent=2))

    position = hype_position(info.user_state(ACCOUNT))
    if not position:
        raise SystemExit("No HYPE position detected after open response; inspect OPEN RESULT")
    actual_size = abs(float(position["szi"]))
    closed = exchange.market_close("HYPE", actual_size, None, 0.01, builder=BUILDER)
    print("CLOSE RESULT", json.dumps(closed, indent=2))

    remaining = hype_position(info.user_state(ACCOUNT))
    if remaining:
        raise SystemExit(f"WARNING: HYPE position remains open: {remaining}")
    print("SUCCESS: minimum HYPE position opened and fully closed with Entropy attribution.")

if __name__ == "__main__":
    main()
