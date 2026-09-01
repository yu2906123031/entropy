"""Transactional SQLite lot ledger for Entropy fills and recovery."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import sqlite3

from .quote_model import Inventory


@dataclass(frozen=True)
class Fill:
    trade_id: str
    side: str
    quantity: float
    price: float
    fee: float = 0.0
    timestamp_ms: int = 0


@dataclass(frozen=True)
class FillResult:
    applied: bool
    realized_pnl: float
    inventory: Inventory


@dataclass(frozen=True)
class LedgerSnapshot:
    inventory: Inventory
    realized_pnl: float
    fees: float
    trade_count: int


class LotLedger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lots (
                    lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    side TEXT NOT NULL CHECK(side IN ('long', 'short')),
                    remaining TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    opened_trade_id TEXT NOT NULL REFERENCES trades(trade_id),
                    CHECK(CAST(remaining AS REAL) > 0)
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _decimal(value: float | str) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("fill values must be finite")
        return result

    def apply_fill(self, fill: Fill) -> FillResult:
        side = fill.side.lower()
        quantity = self._decimal(fill.quantity)
        price = self._decimal(fill.price)
        fee = self._decimal(fill.fee)
        if not fill.trade_id:
            raise ValueError("trade_id is required")
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT realized_pnl FROM trades WHERE trade_id = ?", (fill.trade_id,)
            ).fetchone()
            if duplicate:
                snapshot = self._snapshot(db)
                db.commit()
                return FillResult(False, float(duplicate["realized_pnl"]), snapshot.inventory)

            db.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fill.trade_id, side, str(quantity), str(price), str(fee), "0", fill.timestamp_ms),
            )
            remaining = quantity
            realized = -fee
            closing_side = "short" if side == "buy" else "long"
            lots = db.execute(
                "SELECT lot_id, remaining, entry_price FROM lots WHERE side = ? ORDER BY lot_id",
                (closing_side,),
            ).fetchall()
            for lot in lots:
                if remaining <= 0:
                    break
                lot_remaining = Decimal(lot["remaining"])
                entry = Decimal(lot["entry_price"])
                closed = min(remaining, lot_remaining)
                realized += (entry - price) * closed if side == "buy" else (price - entry) * closed
                remainder = lot_remaining - closed
                if remainder == 0:
                    db.execute("DELETE FROM lots WHERE lot_id = ?", (lot["lot_id"],))
                else:
                    db.execute(
                        "UPDATE lots SET remaining = ? WHERE lot_id = ?",
                        (str(remainder), lot["lot_id"]),
                    )
                remaining -= closed

            if remaining > 0:
                opening_side = "long" if side == "buy" else "short"
                db.execute(
                    "INSERT INTO lots(side, remaining, entry_price, opened_trade_id) VALUES (?, ?, ?, ?)",
                    (opening_side, str(remaining), str(price), fill.trade_id),
                )
            db.execute(
                "UPDATE trades SET realized_pnl = ? WHERE trade_id = ?",
                (str(realized), fill.trade_id),
            )
            snapshot = self._snapshot(db)
            db.commit()
            return FillResult(True, float(realized), snapshot.inventory)

    def _snapshot(self, db: sqlite3.Connection) -> LedgerSnapshot:
        positions = {"long": Decimal("0"), "short": Decimal("0")}
        for row in db.execute("SELECT side, remaining FROM lots"):
            positions[row["side"]] += Decimal(row["remaining"])
        totals = db.execute(
            "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) AS pnl, "
            "COALESCE(SUM(CAST(fee AS REAL)), 0) AS fees, COUNT(*) AS count FROM trades"
        ).fetchone()
        return LedgerSnapshot(
            inventory=Inventory(long=float(positions["long"]), short=float(positions["short"])),
            realized_pnl=float(totals["pnl"]),
            fees=float(totals["fees"]),
            trade_count=int(totals["count"]),
        )

    def snapshot(self) -> LedgerSnapshot:
        with self._connect() as db:
            return self._snapshot(db)

    def get_metadata(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return None if row is None else str(row["value"])

    def set_metadata(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
