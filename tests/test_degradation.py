import unittest

from entropy_mm.degradation import DegradationController, HealthSignal, StrategyState


class DegradationTests(unittest.TestCase):
    def test_unknown_execution_halts_immediately(self):
        ctl = DegradationController(recovery_windows=2)
        self.assertEqual(ctl.update(HealthSignal(unknown_execution=True)), StrategyState.HALTED)
        self.assertEqual(ctl.size_multiplier(), 0.0)

    def test_three_api_errors_halt_immediately(self):
        ctl = DegradationController()
        self.assertEqual(ctl.update(HealthSignal(api_errors=3)), StrategyState.HALTED)

    def test_one_sided_toxicity_selects_one_side_mode(self):
        ctl = DegradationController()
        self.assertEqual(ctl.update(HealthSignal(toxic_bid=True)), StrategyState.ONE_SIDE)
        self.assertEqual(ctl.size_multiplier(), 0.5)

    def test_both_sides_toxic_halt(self):
        ctl = DegradationController()
        self.assertEqual(ctl.update(HealthSignal(toxic_bid=True, toxic_ask=True)), StrategyState.HALTED)
        self.assertEqual(ctl.size_multiplier(), 0.0)

    def test_recovery_is_hysteretic_all_the_way_to_normal(self):
        ctl = DegradationController(recovery_windows=2)
        ctl.update(HealthSignal(unknown_execution=True))
        expected = [
            StrategyState.HALTED,
            StrategyState.ONE_SIDE,
            StrategyState.ONE_SIDE,
            StrategyState.REDUCED,
            StrategyState.REDUCED,
            StrategyState.NORMAL,
        ]
        observed = [ctl.update(HealthSignal()) for _ in expected]
        self.assertEqual(observed, expected)
        self.assertEqual(ctl.size_multiplier(), 1.0)

    def test_new_bad_signal_interrupts_recovery(self):
        ctl = DegradationController(recovery_windows=2)
        ctl.update(HealthSignal(unknown_execution=True))
        ctl.update(HealthSignal())
        self.assertEqual(ctl.update(HealthSignal(api_errors=3)), StrategyState.HALTED)
        self.assertEqual(ctl.healthy_windows, 0)

    def test_bad_markout_reduces(self):
        ctl = DegradationController()
        self.assertEqual(ctl.update(HealthSignal(markout_5s_bps=-5.0)), StrategyState.REDUCED)

    def test_severe_30s_markout_halts(self):
        ctl = DegradationController()
        self.assertEqual(ctl.update(HealthSignal(markout_30s_bps=-8.0)), StrategyState.HALTED)


if __name__ == "__main__":
    unittest.main()
