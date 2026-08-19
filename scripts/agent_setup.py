"""Create an encrypted, dedicated Hyperliquid API/Agent wallet."""
import getpass
import json
from pathlib import Path
from eth_account import Account

KEYSTORE = Path(__file__).resolve().parents[1] / "secrets" / "agent-keystore.json"

def main():
    if KEYSTORE.exists():
        raise SystemExit(f"Refusing to overwrite existing keystore: {KEYSTORE}")
    password = getpass.getpass("New Agent keystore password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Use a password of at least 12 characters")
    agent = Account.create()
    encrypted = Account.encrypt(agent.key, password)
    KEYSTORE.parent.mkdir(parents=True, exist_ok=True)
    KEYSTORE.write_text(json.dumps(encrypted, indent=2), encoding="utf-8")
    print("Agent wallet created and encrypted.")
    print(f"Address: {agent.address}")
    print(f"Keystore: {KEYSTORE}")
    print("Approve this address as a named API wallet: entropy-grid")

if __name__ == "__main__":
    main()
