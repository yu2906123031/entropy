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


if __name__ == "__main__":
    unittest.main()
