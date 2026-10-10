from __future__ import annotations

import json
import fcntl
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from entropy_bot.errors import LiveGuardError


class IntentWAL:
    """Durable pre-submit order intent journal keyed by CLOID."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        os.fchmod(self._lock_fd, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            self._lock_fd = -1
            raise LiveGuardError(f"intent database already in use: {self.path}") from exc
        try:
            self.db = sqlite3.connect(self.path)
            os.chmod(self.path, 0o600)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS intents (
                    cloid TEXT PRIMARY KEY,
                    coin TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('B','A')),
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','resting','filled','rejected','cancelled')),
                    oid INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            self.db.commit()
            check = self.db.execute("PRAGMA integrity_check").fetchone()
            if check != ("ok",):
                raise sqlite3.DatabaseError("intent WAL integrity check failed")
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self._release_lock()
            raise

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
            del self.db
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock_fd >= 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = -1

    def add_pending(self, cloid: str, coin: str, side: str, payload: dict[str, Any]) -> None:
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO intents VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)",
                (cloid, coin, side, json.dumps(payload, sort_keys=True, separators=(",", ":")), now, now),
            )

    def resolve(self, cloid: str, state: str, oid: int | None = None) -> None:
        if state not in {"resting", "filled", "rejected", "cancelled"}:
            raise ValueError(f"invalid terminal intent state {state!r}")
        with self.db:
            cur = self.db.execute(
                "UPDATE intents SET state=?, oid=?, updated_at=? WHERE cloid=?",
                (state, oid, time.time(), cloid),
            )
        if cur.rowcount != 1:
            raise KeyError(cloid)

    def pending(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT cloid, coin, side, payload, created_at FROM intents WHERE state='pending' ORDER BY created_at"
        ).fetchall()
        return [
            {"cloid": r[0], "coin": r[1], "side": r[2], "payload": json.loads(r[3]), "created_at": r[4]}
            for r in rows
        ]

    def active(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT cloid, coin, side, payload, created_at FROM intents "
            "WHERE state IN ('pending','resting') ORDER BY created_at"
        ).fetchall()
        return [
            {"cloid": r[0], "coin": r[1], "side": r[2], "payload": json.loads(r[3]), "created_at": r[4]}
            for r in rows
        ]

    def prune_terminal(self, older_than: float) -> int:
        """Bound WAL growth while retaining unresolved/resting recovery records."""
        with self.db:
            cur = self.db.execute(
                "DELETE FROM intents WHERE state IN ('filled','rejected','cancelled') AND updated_at < ?",
                (float(older_than),),
            )
        return int(cur.rowcount)

    def reconcile_open_orders(self, opens: Iterable[dict[str, Any]]) -> set[str]:
        by_cloid = {
            str(row.get("cloid") or row.get("cloidStr")): row
            for row in opens
            if row.get("cloid") or row.get("cloidStr")
        }
        unresolved: set[str] = set()
        for intent in self.active():
            cloid = intent["cloid"]
            row = by_cloid.get(cloid)
            if row is None:
                unresolved.add(cloid)
                continue
            raw_oid = row.get("oid", row.get("o"))
            oid = int(raw_oid) if raw_oid is not None else None
            self.resolve(cloid, "resting", oid)
        return unresolved

    def reconcile_history(
        self,
        cloids: Iterable[str],
        fills: Iterable[dict[str, Any]],
        order_status: Any,
    ) -> set[str]:
        """Resolve absent pending intents only from authoritative fill/status evidence."""
        fill_ids = {
            str(row.get("cloid") or row.get("cloidStr"))
            for row in fills
            if row.get("cloid") or row.get("cloidStr")
        }
        unresolved: set[str] = set()
        for cloid in cloids:
            if cloid in fill_ids:
                self.resolve(cloid, "filled")
                continue
            status = order_status(cloid)
            if not isinstance(status, dict):
                unresolved.add(cloid)
                continue
            nested = status.get("order")
            order: dict[str, Any] = nested if isinstance(nested, dict) else status
            raw = str(order.get("status") or status.get("status") or "").lower()
            if raw == "filled":
                self.resolve(cloid, "filled")
            elif raw in {"canceled", "cancelled", "rejected", "margincanceled"}:
                self.resolve(cloid, "rejected" if raw == "rejected" else "cancelled")
            else:
                unresolved.add(cloid)
        return unresolved
