import unittest

from entropy_mm.degradation import DegradationController, HealthSignal, StrategyState


class DegradationTests(unittest.TestCase):
    def test_unknown_execution_halts_immediately(self):
        ctl = DegradationController(recovery_windows=2)
        self.assertEqual(ctl.update(HealthSignal(unknown_execution=True)), StrategyState.HALTED)
        self.assertEqual(ctl.size_multiplier(), 0.0)

    def test_one_sided_toxicity_selects_one_side_mode(self):
        ctl = DegradationController()
        self.assertEqual(ctl.update(HealthSignal(toxic_bid=True)), StrategyState.ONE_SIDE)

    def test_recovery_is_hysteretic(self):
        ctl = DegradationController(recovery_windows=2)
        ctl.update(HealthSignal(unknown_execution=True))
        self.assertEqual(ctl.update(HealthSignal()), StrategyState.HALTED)
        self.assertEqual(ctl.update(HealthSignal()), StrategyState.ONE_SIDE)
        self.assertEqual(ctl.update(HealthSignal()), StrategyState.ONE_SIDE)
        self.assertEqual(ctl.update(HealthSignal()), StrategyState.REDUCED)

    def test_bad_markout_reduces(self):
        ctl = DegradationController()
        self.assertEqual(ctl.update(HealthSignal(markout_5s_bps=-5.0)), StrategyState.REDUCED)


if __name__ == "__main__":
    unittest.main()
