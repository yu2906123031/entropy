"""Process lifecycle primitives for the Entropy market-making daemon."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class LoopConfig:
    interval_seconds: float = 2.0
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    max_consecutive_failures: int = 5


@dataclass
class HealthState:
    status: str = "starting"
    pid: int = 0
    started_at_ms: int = 0
    updated_at_ms: int = 0
    completed_cycles: int = 0
    consecutive_failures: int = 0
    last_cycle_started_ms: int | None = None
    last_cycle_completed_ms: int | None = None
    last_error: str | None = None
    stop_reason: str | None = None


class AlreadyRunning(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise AlreadyRunning(f"lock already held: {self.path}") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def write_health(path: str | Path, health: HealthState) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(health), indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_signal_handlers(stop_event: threading.Event, health: HealthState) -> dict[int, object]:
    previous = {}

    def request_stop(signum, _frame):
        health.stop_reason = signal.Signals(signum).name
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    return previous


def restore_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def run_loop(
    run_cycle: Callable[[], None],
    shutdown: Callable[[], None],
    *,
    health_path: str | Path,
    config: LoopConfig = LoopConfig(),
    stop_event: threading.Event | None = None,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> HealthState:
    if config.interval_seconds < 0 or config.initial_backoff_seconds < 0:
        raise ValueError("intervals must be non-negative")
    if config.max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be positive")

    stop = stop_event or threading.Event()
    started = now_ms()
    health = HealthState(pid=os.getpid(), started_at_ms=started, updated_at_ms=started)
    write_health(health_path, health)

    def pause(seconds: float) -> None:
        if sleep is time.sleep:
            stop.wait(seconds)
        else:
            sleep(seconds)

    try:
        while not stop.is_set() and (max_cycles is None or health.completed_cycles < max_cycles):
            health.status = "running"
            health.last_cycle_started_ms = now_ms()
            health.updated_at_ms = health.last_cycle_started_ms
            write_health(health_path, health)
            try:
                run_cycle()
                health.completed_cycles += 1
                health.consecutive_failures = 0
                health.last_error = None
                health.last_cycle_completed_ms = now_ms()
                health.updated_at_ms = health.last_cycle_completed_ms
                write_health(health_path, health)
                if max_cycles is None or health.completed_cycles < max_cycles:
                    pause(config.interval_seconds)
            except Exception as exc:
                health.consecutive_failures += 1
                health.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                health.status = "degraded"
                health.updated_at_ms = now_ms()
                write_health(health_path, health)
                if health.consecutive_failures >= config.max_consecutive_failures:
                    health.stop_reason = "failure_limit"
                    break
                delay = min(
                    config.initial_backoff_seconds * (2 ** (health.consecutive_failures - 1)),
                    config.max_backoff_seconds,
                )
                pause(delay)
    finally:
        health.status = "stopping"
        health.updated_at_ms = now_ms()
        write_health(health_path, health)
        try:
            shutdown()
        except Exception as exc:
            health.last_error = f"shutdown {type(exc).__name__}: {exc}"[:1000]
            health.stop_reason = health.stop_reason or "shutdown_error"
        health.status = "stopped"
        health.stop_reason = health.stop_reason or ("max_cycles" if max_cycles is not None else "requested")
        health.updated_at_ms = now_ms()
        write_health(health_path, health)
    return health
