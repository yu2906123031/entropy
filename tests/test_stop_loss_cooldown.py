from __future__ import annotations

import json

from entropy_bot.cooldown import StopLossCooldown


def test_first_stop_loss_pauses_for_30_minutes(tmp_path):
    tracker = StopLossCooldown(tmp_path / "cooldown.json", 1800, 7200, 7200)
    until = tracker.record(1_000.0)
    assert until == 2_800.0
    assert tracker.active(2_799.0)
    assert not tracker.active(2_800.0)


def test_second_stop_loss_within_window_pauses_for_two_hours(tmp_path):
    path = tmp_path / "cooldown.json"
    tracker = StopLossCooldown(path, 1800, 7200, 7200)
    tracker.record(1_000.0)
    until = tracker.record(3_000.0)
    assert until == 10_200.0
    assert tracker.active(10_199.0)
    saved = json.loads(path.read_text())
    assert saved["consecutive"] == 2


def test_expired_sequence_starts_again_at_30_minutes(tmp_path):
    tracker = StopLossCooldown(tmp_path / "cooldown.json", 1800, 7200, 7200)
    tracker.record(1_000.0)
    assert tracker.record(9_001.0) == 10_801.0


def test_state_survives_restart(tmp_path):
    path = tmp_path / "cooldown.json"
    StopLossCooldown(path, 1800, 7200, 7200).record(1_000.0)
    tracker = StopLossCooldown(path, 1800, 7200, 7200)
    assert tracker.active(2_000.0)
    assert tracker.record(3_000.0) == 10_200.0
