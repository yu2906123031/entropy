#!/usr/bin/env python3
"""Import account2 API-wallet key without storing plaintext."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import subprocess
import tempfile

from eth_account import Account

KEYSTORE = Path("/etc/entropy-bot/account2-agent-keystore.json")
CREDENTIAL = Path("/etc/credstore.encrypted/entropy-account2-grid.cred")
ENV_FILE = Path("/etc/entropy-bot/account2.env")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("请使用 sudo 运行")
    raw = getpass.getpass("输入第二个账户的 API 钱包私钥（隐藏输入）: ").strip()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        raise SystemExit("私钥必须是 32 字节十六进制")
    try:
        key = bytes.fromhex(raw)
        signer = Account.from_key(key)
    except Exception as exc:
        raise SystemExit("私钥格式无效") from exc

    master = input("输入持有资金的第二个主账户地址: ").strip()
    if not master.startswith("0x") or len(master) != 42:
        raise SystemExit("主账户地址格式无效")

    password = getpass.getpass("设置 Keystore 密码（隐藏输入）: ")
    confirm = getpass.getpass("再次输入 Keystore 密码: ")
    if password != confirm or len(password) < 16:
        raise SystemExit("两次密码必须一致，且至少16个字符")

    KEYSTORE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIAL.parent.mkdir(parents=True, exist_ok=True)
    encrypted = Account.encrypt(key, password)
    tmp_keystore = KEYSTORE.with_suffix(".json.new")
    tmp_keystore.write_text(json.dumps(encrypted), encoding="utf-8")
    os.chmod(tmp_keystore, 0o640)

    with tempfile.NamedTemporaryFile(mode="w", dir="/root/.hermes/cache/scratch", delete=False) as fh:
        fh.write(password)
        password_path = fh.name
    os.chmod(password_path, 0o600)
    tmp_credential = CREDENTIAL.with_suffix(".cred.new")
    try:
        subprocess.run(
            ["systemd-creds", "encrypt", "--name=cred", password_path, str(tmp_credential)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        os.chmod(tmp_credential, 0o600)
        os.replace(tmp_keystore, KEYSTORE)
        os.replace(tmp_credential, CREDENTIAL)
    finally:
        Path(password_path).unlink(missing_ok=True)
        tmp_keystore.unlink(missing_ok=True)
        tmp_credential.unlink(missing_ok=True)

    ENV_FILE.write_text(f"LIVE=1\nHYPERLIQUID_ACCOUNT={master}\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o640)
    subprocess.run(["chown", "root:entropy-bot", str(KEYSTORE), str(ENV_FILE)], check=True)
    print(f"已加密保存；API钱包地址: {signer.address}")
    print(f"主账户地址: {master}")
    print("账号2服务保持停止，等待授权与余额核验。")


if __name__ == "__main__":
    main()
