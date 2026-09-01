"""Automatic fail-closed strategy degradation and hysteretic recovery."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class StrategyState(IntEnum):
    NORMAL = 0
    REDUCED = 1
    ONE_SIDE = 2
    HALTED = 3


@dataclass(frozen=True)
class HealthSignal:
    markout_5s_bps: float | None = None
    markout_30s_bps: float | None = None
    pnl_slope_usd: float = 0.0
    api_errors: int = 0
    unknown_execution: bool = False
    toxic_bid: bool = False
    toxic_ask: bool = False


@dataclass
class DegradationController:
    state: StrategyState = StrategyState.NORMAL
    healthy_windows: int = 0
    recovery_windows: int = 5

    def update(self, signal: HealthSignal) -> StrategyState:
        bilateral_toxicity = signal.toxic_bid and signal.toxic_ask
        severe = (
            signal.unknown_execution
            or signal.api_errors >= 3
            or bilateral_toxicity
            or (signal.markout_30s_bps is not None and signal.markout_30s_bps <= -8.0)
        )
        bad = (
            signal.api_errors >= 1
            or signal.pnl_slope_usd < -0.02
            or (signal.markout_5s_bps is not None and signal.markout_5s_bps <= -4.0)
        )
        one_side = signal.toxic_bid ^ signal.toxic_ask
        if severe:
            target = StrategyState.HALTED
        elif one_side:
            target = StrategyState.ONE_SIDE
        elif bad:
            target = StrategyState.REDUCED
        else:
            target = StrategyState.NORMAL

        if target > self.state:
            self.state = target
            self.healthy_windows = 0
        elif target < self.state:
            self.healthy_windows += 1
            if self.healthy_windows >= self.recovery_windows:
                self.state = StrategyState(self.state - 1)
                self.healthy_windows = 0
        else:
            self.healthy_windows = 0 if target != StrategyState.NORMAL else self.healthy_windows + 1
        return self.state

    def size_multiplier(self) -> float:
        return {
            StrategyState.NORMAL: 1.0,
            StrategyState.REDUCED: 0.5,
            StrategyState.ONE_SIDE: 0.5,
            StrategyState.HALTED: 0.0,
        }[self.state]
