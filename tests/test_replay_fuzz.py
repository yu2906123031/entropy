import math
import random
import unittest

from entropy_mm.replay import ReplaySnapshot, replay_quotes


def make_book(mid: float, spread_bps: float, rng: random.Random, levels: int = 5):
    half = mid * spread_bps / 20_000.0
    bids = []
    asks = []
    for i in range(levels):
        step = mid * (0.5 + i) / 10_000.0
        bids.append({"px": f"{mid - half - step:.8f}", "sz": f"{0.1 + rng.random() * 10:.8f}"})
        asks.append({"px": f"{mid + half + step:.8f}", "sz": f"{0.1 + rng.random() * 10:.8f}"})
    return [bids, asks]


class ReplayFuzzTests(unittest.TestCase):
    def test_thousand_snapshot_replay_remains_finite_and_bounded(self):
        rng = random.Random(2906123031)
        mid = 100.0
        rows = []
        for i in range(1000):
            mid *= 1.0 + rng.uniform(-8, 8) / 10_000.0
            rows.append(
                ReplaySnapshot(
                    i * 2000,
                    make_book(mid, rng.uniform(1.0, 20.0), rng),
                    volatility_bps=rng.uniform(0, 40),
                    directional_bps=rng.uniform(-45, 45),
                    funding_bps=rng.uniform(-35, 35),
                )
            )
        result = replay_quotes(rows)
        self.assertEqual(result.snapshots, 1000)
        self.assertLessEqual(result.quoted, result.snapshots)
        self.assertLessEqual(result.paused_bid, result.snapshots)
        self.assertLessEqual(result.paused_ask, result.snapshots)
        self.assertTrue(math.isfinite(result.mean_required_edge_bps))
        self.assertTrue(math.isfinite(result.mean_half_spread_bps))
        self.assertGreater(result.mean_required_edge_bps, 0)
        self.assertGreater(result.mean_half_spread_bps, 0)

    def test_replay_is_deterministic_for_same_inputs(self):
        rng = random.Random(42)
        rows = [ReplaySnapshot(i, make_book(100 + i * 0.01, 5, rng), volatility_bps=3) for i in range(100)]
        self.assertEqual(replay_quotes(rows), replay_quotes(rows))

    def test_extreme_but_finite_inputs_fail_closed_or_remain_finite(self):
        rng = random.Random(7)
        rows = [ReplaySnapshot(1, make_book(100, 100, rng), volatility_bps=500, directional_bps=500, funding_bps=500)]
        result = replay_quotes(rows)
        self.assertEqual(result.snapshots, 1)
        self.assertEqual(result.paused_bid + result.paused_ask >= 1, True)
        self.assertTrue(math.isfinite(result.mean_required_edge_bps))


if __name__ == "__main__":
    unittest.main()
