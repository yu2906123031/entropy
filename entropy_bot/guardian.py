"""Independent, bounded-failure EBOT cancellation guardian."""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import signal
import time
from pathlib import Path
from typing import Callable

from entropy_bot.coins import ALLOWED_COINS, ALLOWED_DEX
from entropy_bot.config import Settings, require_signer
from entropy_bot.orders import LiveSigner, action_statuses_ok, is_bot_cloid
from entropy_bot.rest import InfoClient
from entropy_bot.safety import atomic_json
from entropy_bot.status import load_io_markets

log = logging.getLogger("entropy_bot.guardian")
MAX_CONSECUTIVE_FAILURES = 3


def health_transition(ok: bool, failures: int) -> tuple[int, int, bool]:
    """Return rc, consecutive failures and whether supervision should restart."""
    count = 0 if ok else failures + 1
    return (0 if ok else 2), count, count >= MAX_CONSECUTIVE_FAILURES


def final_return_code(rc: int, cancel_ok: bool) -> int:
    return rc if cancel_ok else 2


def lease_fresh(path: str, account: str, max_age: float, now: float) -> bool:
    try:
        row = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        ts = float(row["heartbeat"])
        return (str(row.get("account", "")).lower() == account.lower() and row.get("dex") == "io"
                and math.isfinite(ts) and 0 <= now - ts <= max_age and ts <= now + 1.0)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _identity() -> tuple[int, str, str]:
    pid = os.getpid()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    start = Path(f"/proc/{pid}/stat").read_text().split()[21]
    return pid, boot, start


def _write_token(path: str, token: str) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(token + "\n", encoding="ascii")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def cancel_ebot(client: InfoClient, signer: LiveSigner, markets: dict) -> bool:
    opens = client.open_orders(signer.account, ALLOWED_DEX, optional=True)
    if opens is None:
        return False
    pairs = []
    for row in opens:
        coin = row.get("coin")
        cloid = row.get("cloid") or row.get("cloidStr")
        if coin in ALLOWED_COINS and coin in markets and is_bot_cloid(cloid):
            pairs.append((markets[coin].asset_id, cloid))
    if not pairs:
        return True
    try:
        resp = client.post_exchange(signer.signed_cancel_cloids(pairs))
    except Exception as exc:  # guardian converts transport failures to readiness state
        log.error("EBOT cancellation failed: %s", exc)
        return False
    return action_statuses_ok(resp, len(pairs))


def run_guardian(
    settings: Settings,
    *,
    seconds: float | None = None,
    client_factory: Callable[[str], InfoClient] = InfoClient,
    signer_factory: Callable[..., LiveSigner] = LiveSigner,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    require_signer(settings)
    signer = signer_factory(settings.private_key or "", settings.account, nonce_dir=settings.state_dir)
    client = client_factory(settings.api_url)
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    deadline = time.time() + seconds if seconds else None
    rc = 0
    token = secrets.token_urlsafe(32)
    pid, boot_id, start_id = _identity()
    markets: dict = {}
    failures = 0
    try:
        _write_token(settings.guardian_token_path, token)
        _, markets, _ = load_io_markets(client, ALLOWED_COINS)
        if not cancel_ebot(client, signer, markets):
            rc = 2
        else:
            while not stop["flag"] and (deadline is None or time.time() < deadline):
                now = time.time()
                ok = True
                if not lease_fresh(settings.guardian_lease_path, signer.account, settings.guardian_max_age, now):
                    ok = cancel_ebot(client, signer, markets)
                rc, failures, exhausted = health_transition(ok, failures)
                atomic_json(settings.guardian_ready_path, {
                    "ready": ok, "heartbeat": now, "account": signer.account, "dex": "io",
                    "pid": pid, "boot_id": boot_id, "start_id": start_id, "session_token": token,
                })
                if exhausted:
                    break
                sleep(settings.guardian_poll_s)
    finally:
        atomic_json(settings.guardian_ready_path, {
            "ready": False, "heartbeat": time.time(), "account": signer.account, "dex": "io",
            "pid": pid, "boot_id": boot_id, "start_id": start_id, "session_token": token,
        })
        rc = final_return_code(rc, cancel_ebot(client, signer, markets))
        client.close()
    return rc
