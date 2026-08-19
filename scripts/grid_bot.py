"""Small live HYPE grid with Entropy attribution and conservative risk exits."""
import getpass
import json
import signal
import time
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

ACCOUNT = "0x78605485604BA45ce0eF860DB1594ec810154477"
AGENT = "0x3B4347B99BB749eBdD6DE720736796E1b7Dfe4a6"
BUILDER = {"b": "0xcd254d2a328f7f67c7c6fef930a4757516f7b601", "f": 0}
KEYSTORE = Path(__file__).resolve().parents[1] / "secrets" / "agent-keystore.json"
LEVELS = [52.44, 53.61, 54.77, 55.94, 57.10, 58.27, 59.43, 60.60, 61.76, 62.93, 64.09]
LOWER, UPPER = LEVELS[0], LEVELS[-1]
SIZE = 0.20
MAX_POSITION = 1.001
POLL_SECONDS = 2

running = True

def stop_requested(*_):
    global running
    running = False

def position(info):
    for item in info.user_state(ACCOUNT).get("assetPositions", []):
        p = item.get("position", {})
        if p.get("coin") == "HYPE" and abs(float(p.get("szi", 0) or 0)) > 0:
            return p
    return None

def cancel_hype(exchange, info):
    for order in info.open_orders(ACCOUNT):
        if order.get("coin") == "HYPE":
            try:
                print("CANCEL", order["oid"], exchange.cancel("HYPE", order["oid"]))
            except Exception as exc:
                print("CANCEL ERROR", order.get("oid"), exc)

def flatten(exchange, info):
    p = position(info)
    if p:
        print("FLATTEN", exchange.market_close("HYPE", abs(float(p["szi"])), None, 0.02, builder=BUILDER))

def place(exchange, is_buy, price):
    result = exchange.order("HYPE", is_buy, SIZE, price, {"limit": {"tif": "Alo"}}, False, builder=BUILDER)
    print("PLACE", "BUY" if is_buy else "SELL", price, json.dumps(result))
    status = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
    if isinstance(status, dict) and status.get("error"):
        raise RuntimeError(status["error"])

def has_order_at(info, is_buy, price):
    side = "B" if is_buy else "A"
    return any(o.get("coin") == "HYPE" and o.get("side") == side and abs(float(o["limitPx"]) - price) < 0.0001 for o in info.open_orders(ACCOUNT))

def cancel_at(exchange, info, price):
    for order in info.open_orders(ACCOUNT):
        if order.get("coin") == "HYPE" and abs(float(order["limitPx"]) - price) < 0.0001:
            exchange.cancel("HYPE", order["oid"])

def nearest_index(price):
    return min(range(len(LEVELS)), key=lambda i: abs(LEVELS[i] - price))

def main():
    password = getpass.getpass("Agent keystore password (hidden): ")
    key = Account.decrypt(json.loads(KEYSTORE.read_text(encoding="utf-8")), password)
    agent = Account.from_key(key)
    if agent.address.lower() != AGENT.lower():
        raise SystemExit(f"Agent mismatch: {agent.address}")
    info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=30)
    role = info.user_role(agent.address)
    if role.get("role") != "agent" or role.get("data", {}).get("user", "").lower() != ACCOUNT.lower():
        raise SystemExit(f"Agent authorization mismatch: {role}")
    if position(info):
        raise SystemExit("Refusing startup with an existing HYPE position")
    if any(o.get("coin") == "HYPE" for o in info.open_orders(ACCOUNT)):
        raise SystemExit("Refusing startup with existing HYPE orders")

    exchange = Exchange(agent, constants.MAINNET_API_URL, account_address=ACCOUNT, timeout=30)
    exchange.update_leverage(2, "HYPE", True)
    initial_tids = {x["tid"] for x in info.user_fills(ACCOUNT)}
    print(json.dumps({"range": [LOWER, UPPER], "levels": LEVELS, "size": SIZE, "leverage": 2, "maxPosition": MAX_POSITION, "builder": BUILDER}, indent=2))
    if input('Type exactly "START GRID": ') != "START GRID":
        raise SystemExit("Grid not started")

    signal.signal(signal.SIGINT, stop_requested)
    signal.signal(signal.SIGTERM, stop_requested)
    try:
        for px in LEVELS[:5]:
            place(exchange, True, px)
        for px in LEVELS[6:]:
            place(exchange, False, px)
        print("GRID RUNNING - keep this window open; Ctrl+C performs safe shutdown")

        seen = initial_tids
        while running:
            mid = float(info.all_mids()["HYPE"])
            p = position(info)
            pos_size = abs(float(p["szi"])) if p else 0.0
            if mid <= LOWER or mid >= UPPER or pos_size > MAX_POSITION:
                print("RISK EXIT", {"mid": mid, "position": pos_size})
                break
            fills = [x for x in info.user_fills(ACCOUNT) if x["tid"] not in seen and x.get("coin") == "HYPE"]
            for fill in reversed(fills):
                seen.add(fill["tid"])
                idx = nearest_index(float(fill["px"]))
                is_buy_fill = fill["side"] == "B"
                target_idx = idx + 1 if is_buy_fill else idx - 1
                if not 0 <= target_idx < len(LEVELS):
                    continue
                target = LEVELS[target_idx]
                new_is_buy = not is_buy_fill
                cancel_at(exchange, info, target)
                if not has_order_at(info, new_is_buy, target):
                    place(exchange, new_is_buy, target)
                print("CYCLE", {"fill": fill, "replacement": {"buy": new_is_buy, "price": target}})
            time.sleep(POLL_SECONDS)
    finally:
        print("SAFE SHUTDOWN: cancelling HYPE orders and flattening position")
        cancel_hype(exchange, info)
        flatten(exchange, info)
        print("STOPPED")

if __name__ == "__main__":
    main()
