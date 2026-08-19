"""Read-only Hyperliquid account and Entropy builder checks."""
import json
import os
import sys
from hyperliquid.info import Info
from hyperliquid.utils import constants

BUILDER = os.getenv("ENTROPY_BUILDER_ADDRESS", "0xcD254d2A328f7f67C7c6FEf930A4757516F7b601")

def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/account_check.py 0xYOUR_MAIN_ACCOUNT")
    address = sys.argv[1]
    if not address.startswith("0x") or len(address) != 42:
        raise SystemExit("Expected a 42-character 0x account address")
    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    state = info.user_state(address)
    open_orders = info.open_orders(address)
    max_builder_fee = info.post("/info", {"type": "maxBuilderFee", "user": address, "builder": BUILDER})
    positions = [x.get("position", {}) for x in state.get("assetPositions", []) if float(x.get("position", {}).get("szi", 0) or 0) != 0]
    result = {"account": address, "accountValue": state.get("marginSummary", {}).get("accountValue"), "withdrawable": state.get("withdrawable"), "positions": positions, "openOrders": open_orders, "entropyBuilder": BUILDER, "maxEntropyBuilderFee": max_builder_fee}
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
