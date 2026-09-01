import tempfile
import unittest
from pathlib import Path

from entropy_mm.fill_sync import sync_fills
from entropy_mm.ledger import LotLedger


class FakeInfo:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def user_fills_by_time(self, address, start_time, end_time=None, aggregate_by_time=False):
        self.calls.append((address, start_time, end_time, aggregate_by_time))
        return [row for row in self.rows if start_time <= row["time"] <= end_time]


def fill(tid, time, side="B", coin="HYPE", size="1", price="100", fee="0"):
    return {"tid": tid, "time": time, "side": side, "coin": coin, "sz": size, "px": price, "fee": fee}


class FillSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = LotLedger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_filters_coin_orders_fills_and_advances_cursor(self):
        client = FakeInfo([
            fill(3, 3000, side="A", size="0.4", price="110"),
            fill(1, 1000, size="1"),
            fill(2, 2000, coin="BTC"),
        ])
        result = sync_fills(
            client, self.ledger, address="0xabc", coin="HYPE",
            initial_start_ms=0, end_ms=5000, window_ms=5000,
        )
        self.assertEqual((result.fetched, result.matched, result.applied), (3, 2, 2))
        self.assertAlmostEqual(self.ledger.snapshot().inventory.long, 0.6)
        self.assertEqual(result.cursor_ms, 5000)

    def test_restart_overlap_replays_idempotently_and_catches_new_fill(self):
        client = FakeInfo([fill(1, 9000)])
        first = sync_fills(
            client, self.ledger, address="0xabc", coin="HYPE",
            initial_start_ms=0, end_ms=10000, window_ms=10000,
        )
        client.rows.append(fill(2, 10500, side="A"))
        second = sync_fills(
            client, self.ledger, address="0xabc", coin="HYPE",
            initial_start_ms=0, end_ms=11000, window_ms=10000, overlap_ms=2000,
        )
        self.assertEqual(first.applied, 1)
        self.assertEqual((second.applied, second.duplicates), (1, 1))
        self.assertEqual(self.ledger.snapshot().trade_count, 2)
        self.assertAlmostEqual(self.ledger.snapshot().inventory.long, 0)

    def test_api_limit_fails_closed_without_advancing_cursor(self):
        client = FakeInfo([fill(1, 1000), fill(2, 2000)])
        with self.assertRaises(RuntimeError):
            sync_fills(
                client, self.ledger, address="0xabc", coin="HYPE",
                initial_start_ms=0, end_ms=3000, api_row_limit=2,
            )
        self.assertIsNone(self.ledger.get_metadata("hyperliquid_fill_cursor:HYPE"))
        self.assertEqual(self.ledger.snapshot().trade_count, 0)

    def test_invalid_side_stops_before_cursor_advance(self):
        client = FakeInfo([fill(1, 1000, side="X")])
        with self.assertRaises(ValueError):
            sync_fills(
                client, self.ledger, address="0xabc", coin="HYPE",
                initial_start_ms=0, end_ms=2000,
            )
        self.assertIsNone(self.ledger.get_metadata("hyperliquid_fill_cursor:HYPE"))


if __name__ == "__main__":
    unittest.main()
