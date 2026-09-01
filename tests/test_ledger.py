import tempfile
import unittest
from pathlib import Path

from entropy_mm.ledger import Fill, LotLedger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = LotLedger(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_trade_id_is_idempotent(self):
        fill = Fill("t1", "buy", 1, 100, fee=0.1)
        first = self.ledger.apply_fill(fill)
        second = self.ledger.apply_fill(fill)
        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertEqual(self.ledger.snapshot().trade_count, 1)
        self.assertAlmostEqual(second.inventory.long, 1)

    def test_sell_consumes_long_fifo_and_realizes_pnl(self):
        self.ledger.apply_fill(Fill("b1", "buy", 1, 100))
        self.ledger.apply_fill(Fill("b2", "buy", 1, 110))
        result = self.ledger.apply_fill(Fill("s1", "sell", 1.5, 120, fee=1))
        self.assertAlmostEqual(result.realized_pnl, 24)
        snapshot = self.ledger.snapshot()
        self.assertAlmostEqual(snapshot.inventory.long, 0.5)
        self.assertAlmostEqual(snapshot.realized_pnl, 24)
        self.assertAlmostEqual(snapshot.fees, 1)

    def test_buy_consumes_short_and_can_flip_long(self):
        self.ledger.apply_fill(Fill("s1", "sell", 1, 120))
        result = self.ledger.apply_fill(Fill("b1", "buy", 1.5, 100))
        self.assertAlmostEqual(result.realized_pnl, 20)
        self.assertAlmostEqual(result.inventory.short, 0)
        self.assertAlmostEqual(result.inventory.long, 0.5)

    def test_partial_close_preserves_remaining_lot(self):
        self.ledger.apply_fill(Fill("b1", "buy", 2, 100))
        self.ledger.apply_fill(Fill("s1", "sell", 0.75, 105))
        self.assertAlmostEqual(self.ledger.snapshot().inventory.long, 1.25)

    def test_reopening_database_recovers_state(self):
        self.ledger.apply_fill(Fill("b1", "buy", 0.4, 100))
        reopened = LotLedger(self.path)
        snapshot = reopened.snapshot()
        self.assertAlmostEqual(snapshot.inventory.long, 0.4)
        self.assertEqual(snapshot.trade_count, 1)

    def test_invalid_fill_rolls_back_without_trade(self):
        with self.assertRaises(ValueError):
            self.ledger.apply_fill(Fill("bad", "hold", 1, 100))
        self.assertEqual(self.ledger.snapshot().trade_count, 0)

    def test_negative_fee_is_recorded_as_maker_rebate(self):
        result = self.ledger.apply_fill(Fill("rebate", "buy", 1, 100, fee=-0.02))
        self.assertAlmostEqual(result.realized_pnl, 0.02)
        self.assertAlmostEqual(self.ledger.snapshot().fees, -0.02)


if __name__ == "__main__":
    unittest.main()
