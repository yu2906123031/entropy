import random
import unittest

from entropy_mm.degradation import DegradationController, HealthSignal, StrategyState


class DegradationFuzzTests(unittest.TestCase):
    def test_random_health_sequences_never_leave_valid_state_space(self):
        rng = random.Random(20260901)
        ctl = DegradationController(recovery_windows=3)
        states = set(StrategyState)
        for _ in range(5000):
            signal = HealthSignal(
                markout_5s_bps=rng.choice([None, rng.uniform(-12, 6)]),
                markout_30s_bps=rng.choice([None, rng.uniform(-15, 8)]),
                pnl_slope_usd=rng.uniform(-0.08, 0.05),
                api_errors=rng.randrange(0, 5),
                unknown_execution=rng.random() < 0.02,
                toxic_bid=rng.random() < 0.15,
                toxic_ask=rng.random() < 0.15,
            )
            state = ctl.update(signal)
            self.assertIn(state, states)
            self.assertGreaterEqual(ctl.size_multiplier(), 0.0)
            self.assertLessEqual(ctl.size_multiplier(), 1.0)

    def test_severe_signal_cannot_improve_state(self):
        for initial in StrategyState:
            ctl = DegradationController(state=initial, recovery_windows=1)
            state = ctl.update(HealthSignal(unknown_execution=True))
            self.assertEqual(state, StrategyState.HALTED)

    def test_recovery_from_halted_requires_full_hysteresis_each_step(self):
        ctl = DegradationController(recovery_windows=3)
        ctl.update(HealthSignal(unknown_execution=True))
        expected = [
            StrategyState.HALTED, StrategyState.HALTED, StrategyState.ONE_SIDE,
            StrategyState.ONE_SIDE, StrategyState.ONE_SIDE, StrategyState.REDUCED,
            StrategyState.REDUCED, StrategyState.REDUCED, StrategyState.NORMAL,
        ]
        actual = [ctl.update(HealthSignal()) for _ in range(9)]
        self.assertEqual(actual, expected)

    def test_new_bad_window_resets_recovery_counter(self):
        ctl = DegradationController(recovery_windows=3)
        ctl.update(HealthSignal(unknown_execution=True))
        ctl.update(HealthSignal())
        ctl.update(HealthSignal())
        self.assertEqual(ctl.healthy_windows, 2)
        ctl.update(HealthSignal(unknown_execution=True))
        self.assertEqual(ctl.state, StrategyState.HALTED)
        self.assertEqual(ctl.healthy_windows, 0)


if __name__ == "__main__":
    unittest.main()
