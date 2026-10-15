"""Persistent opening cooldown after emergency stop-loss fills."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class StopLossCooldown:
    def __init__(
        self,
        path: str | Path,
        first_pause_s: float,
        second_pause_s: float,
        sequence_window_s: float,
    ) -> None:
        self.path = Path(path)
        self.first_pause_s = float(first_pause_s)
        self.second_pause_s = float(second_pause_s)
        self.sequence_window_s = float(sequence_window_s)
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                "last_stop_at": float(data.get("last_stop_at", 0.0)),
                "cooldown_until": float(data.get("cooldown_until", 0.0)),
                "consecutive": max(0, int(data.get("consecutive", 0))),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"last_stop_at": 0.0, "cooldown_until": 0.0, "consecutive": 0}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".new")
        tmp.write_text(json.dumps(self._state, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def active(self, now: float) -> bool:
        return float(now) < float(self._state["cooldown_until"])

    @property
    def cooldown_until(self) -> float:
        return float(self._state["cooldown_until"])

    def record(self, now: float) -> float:
        now = float(now)
        last = float(self._state["last_stop_at"])
        consecutive = int(self._state["consecutive"])
        if last <= 0 or now - last > self.sequence_window_s:
            consecutive = 1
        else:
            consecutive += 1
        pause = self.second_pause_s if consecutive >= 2 else self.first_pause_s
        self._state = {
            "last_stop_at": now,
            "cooldown_until": now + pause,
            "consecutive": consecutive,
        }
        self._save()
        return now + pause
