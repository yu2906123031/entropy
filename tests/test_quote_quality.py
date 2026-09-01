import tempfile
import unittest
from pathlib import Path

from entropy_mm.quote_quality import Exposure, QuoteQualityStore, empirical_fill_multiplier


class QuoteQualityTests(unittest.TestCase):
    def test_persists_fill_and_cancel_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteQualityStore(Path(tmp) / "quality.sqlite3")
            store.open(Exposure(1, "buy", 99.9, 0.02, 1000, 10.0, 0.2))
            store.open(Exposure(2, "buy", 99.8, 0.02, 1000, 20.0, 1.0))
            store.close(1, closed_at_ms=5000, outcome="fill")
            store.close(2, closed_at_ms=6000, outcome="cancel")
            quality = store.quality(side="buy")
            self.assertEqual(quality.samples, 2)
            self.assertEqual(quality.fills, 1)
            self.assertEqual(quality.fill_rate, 0.5)
            self.assertEqual(quality.mean_exposure_ms, 4500)

    def test_unknown_outcomes_do_not_pollute_fill_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteQualityStore(Path(tmp) / "quality.sqlite3")
            store.open(Exposure(1, "sell", 101.0, 0.02, 1000, 5.0, 0.1))
            store.close(1, closed_at_ms=2000, outcome="unknown")
            self.assertEqual(store.quality().samples, 0)

    def test_queue_heavy_history_reduces_learned_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteQualityStore(Path(tmp) / "quality.sqlite3")
            for index in range(20):
                store.open(Exposure(index, "buy", 100.0, 0.01, index * 1000, 2.0, 1.0))
                store.close(index, closed_at_ms=index * 1000 + 1000, outcome="fill" if index < 5 else "cancel")
            multiplier = empirical_fill_multiplier(store.quality(), min_samples=20)
            self.assertLess(multiplier, 1.0)
            self.assertGreaterEqual(multiplier, 0.20)


if __name__ == "__main__":
    unittest.main()
