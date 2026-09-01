"""Incremental Hyperliquid fill ingestion for the Entropy lot ledger."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .ledger import Fill, LotLedger


class FillHistoryClient(Protocol):
    def user_fills_by_time(
        self, address: str, start_time: int, end_time: int | None = None,
        aggregate_by_time: bool = False,
    ) -> list[dict]: ...


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    matched: int
    applied: int
    duplicates: int
    cursor_ms: int


def sync_fills(
    client: FillHistoryClient,
    ledger: LotLedger,
    *,
    address: str,
    coin: str,
    initial_start_ms: int,
    end_ms: int,
    window_ms: int = 86_400_000,
    overlap_ms: int = 2_000,
    api_row_limit: int = 2_000,
) -> SyncResult:
    """Ingest bounded time windows; replay overlap is safe through trade ID uniqueness."""
    if not address or not coin:
        raise ValueError("address and coin are required")
    if initial_start_ms < 0 or end_ms < initial_start_ms:
        raise ValueError("invalid synchronization time range")
    if window_ms <= 0 or overlap_ms < 0:
        raise ValueError("invalid synchronization window")

    key = f"hyperliquid_fill_cursor:{coin}"
    stored = ledger.get_metadata(key)
    cursor = int(stored) if stored is not None else initial_start_ms
    start = max(initial_start_ms, cursor - overlap_ms)
    fetched = matched = applied = duplicates = 0

    while start < end_ms:
        window_end = min(start + window_ms, end_ms)
        rows = client.user_fills_by_time(address, start, window_end, aggregate_by_time=False)
        fetched += len(rows)
        if len(rows) >= api_row_limit:
            raise RuntimeError(
                f"fill window reached API row limit ({api_row_limit}); reduce window_ms"
            )
        relevant = [row for row in rows if row.get("coin") == coin]
        relevant.sort(key=lambda row: (int(row["time"]), str(row["tid"])))
        matched += len(relevant)
        for row in relevant:
            side_code = row.get("side")
            if side_code not in {"B", "A"}:
                raise ValueError(f"unsupported Hyperliquid fill side: {side_code!r}")
            result = ledger.apply_fill(
                Fill(
                    trade_id=f"hl:{coin}:{row['tid']}",
                    side="buy" if side_code == "B" else "sell",
                    quantity=float(row["sz"]),
                    price=float(row["px"]),
                    fee=float(row.get("fee", 0) or 0),
                    timestamp_ms=int(row["time"]),
                )
            )
            if result.applied:
                applied += 1
            else:
                duplicates += 1
        ledger.set_metadata(key, str(window_end))
        cursor = window_end
        start = window_end

    return SyncResult(fetched, matched, applied, duplicates, cursor)
