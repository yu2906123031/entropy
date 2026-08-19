"""Import a Hyperliquid-generated Agent key into an encrypted local keystore."""
import getpass
import json
import sys
from pathlib import Path
from eth_account import Account

KEYSTORE = Path(__file__).resolve().parents[1] / "secrets" / "agent-keystore.json"

def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/agent_import.py 0xEXPECTED_AGENT_ADDRESS")
    expected = sys.argv[1].lower()
    if not expected.startswith("0x") or len(expected) != 42:
        raise SystemExit("Expected a 42-character 0x Agent address")
    if KEYSTORE.exists():
        raise SystemExit(f"Refusing to overwrite existing keystore: {KEYSTORE}")

    private_key = getpass.getpass("Paste Agent private key (hidden): ").strip()
    try:
        agent = Account.from_key(private_key)
    except Exception as exc:
        raise SystemExit("Invalid Agent private key") from exc
    if agent.address.lower() != expected:
        raise SystemExit(f"Private key derives {agent.address}, not expected {sys.argv[1]}")

    password = getpass.getpass("New keystore password (12+ characters): ")
    confirm = getpass.getpass("Confirm keystore password: ")
    if password != confirm:
        raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Use a password of at least 12 characters")

    encrypted = Account.encrypt(agent.key, password)
    KEYSTORE.parent.mkdir(parents=True, exist_ok=True)
    KEYSTORE.write_text(json.dumps(encrypted, indent=2), encoding="utf-8")
    print(f"Imported and encrypted Agent: {agent.address}")
    print(f"Keystore: {KEYSTORE}")
    print("The plaintext private key was not written to disk.")

if __name__ == "__main__":
    main()
