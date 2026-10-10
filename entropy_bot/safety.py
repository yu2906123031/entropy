from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import secrets
import time
from pathlib import Path
from typing import Any

from entropy_bot.errors import LiveGuardError


def account_domain(account: str, dex: str = "io") -> str:
    return hashlib.sha256(f"{account.lower()}:{dex}".encode()).hexdigest()[:24]


class AccountLock:
    """Exclusive process lock for one master-account + DEX coordination domain."""

    def __init__(self, account: str, state_dir: str | Path, dex: str = "io") -> None:
        root = Path(state_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.path = root / f"account-{account_domain(account, dex)}.lock"
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = -1
            raise LiveGuardError("another live process owns this account+DEX") from exc

    def close(self) -> None:
        if self.fd >= 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = -1


class NonceStore:
    """Cross-process, restart-safe monotonic nonce allocator."""

    def __init__(self, account: str, state_dir: str | Path, dex: str = "io") -> None:
        root = Path(state_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / f"nonce-{account_domain(account, dex)}"
        self.lock_path = self.path.with_suffix(".lock")

    def next(self, now_ms: int | None = None) -> int:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            last = 0
            try:
                last = int(self.path.read_text(encoding="ascii").strip())
            except (FileNotFoundError, ValueError):
                pass
            value = max(int(time.time() * 1000) if now_ms is None else int(now_ms), last + 1)
            tmp = self.path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(str(value), encoding="ascii")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            dfd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return value
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def atomic_json(path: str | Path, data: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def _process_identity(pid: int) -> tuple[str, str] | None:
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        start = Path(f"/proc/{pid}/stat").read_text().split()[21]
        return boot, start
    except (OSError, IndexError, ValueError):
        return None


def fresh_guardian(path: str | Path, account: str, *, max_age: float,
                   token_path: str | Path | None = None, now: float | None = None) -> bool:
    try:
        row = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        ts = float(row["heartbeat"])
        current = time.time() if now is None else now
        pid = int(row["pid"])
        if _process_identity(pid) != (row.get("boot_id"), str(row.get("start_id"))):
            return False
        if token_path is not None:
            expected = Path(token_path).expanduser().read_text(encoding="ascii").strip()
            if not expected or not secrets.compare_digest(str(row.get("session_token", "")), expected):
                return False
        return (
            row.get("ready") is True
            and str(row.get("account", "")).lower() == account.lower()
            and row.get("dex") == "io"
            and math.isfinite(ts)
            and 0 <= current - ts <= max_age
            and ts <= current + 1.0
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
