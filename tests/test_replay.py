import unittest

from entropy_mm.replay import ReplaySnapshot, replay_quotes


BOOK = [
    [{"px": "99.9", "sz": "5"}, {"px": "99.8", "sz": "4"}],
    [{"px": "100.1", "sz": "5"}, {"px": "100.2", "sz": "4"}],
]


class ReplayTests(unittest.TestCase):
    def test_replay_produces_quote_statistics(self):
        rows = [ReplaySnapshot(1, BOOK), ReplaySnapshot(2, BOOK, volatility_bps=4.0)]
        result = replay_quotes(rows)
        self.assertEqual(result.snapshots, 2)
        self.assertEqual(result.quoted, 2)
        self.assertGreater(result.mean_required_edge_bps, 0.0)
        self.assertGreater(result.mean_half_spread_bps, 0.0)

    def test_extreme_positive_funding_pauses_bid(self):
        result = replay_quotes([ReplaySnapshot(1, BOOK, funding_bps=30.0)])
        self.assertEqual(result.paused_bid, 1)
        self.assertEqual(result.paused_ask, 0)

    def test_extreme_negative_funding_pauses_ask(self):
        result = replay_quotes([ReplaySnapshot(1, BOOK, funding_bps=-30.0)])
        self.assertEqual(result.paused_bid, 0)
        self.assertEqual(result.paused_ask, 1)

    def test_empty_replay_returns_empty_statistics(self):
        result = replay_quotes([])
        self.assertEqual(result.snapshots, 0)
        self.assertEqual(result.quoted, 0)
        self.assertIsNone(result.mean_required_edge_bps)
        self.assertIsNone(result.mean_half_spread_bps)

    def test_crossed_book_fails_closed(self):
        crossed = [[{"px": "100.2", "sz": "1"}], [{"px": "100.1", "sz": "1"}]]
        with self.assertRaises(ValueError):
            replay_quotes([ReplaySnapshot(1, crossed)])

    def test_empty_side_fails_closed(self):
        with self.assertRaises(ValueError):
            replay_quotes([ReplaySnapshot(1, [[], [{"px": "100.1", "sz": "1"}]])])


if __name__ == "__main__":
    unittest.main()
