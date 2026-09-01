#!/usr/bin/env python3
"""Supervise repeated read-only Entropy market-making cycles."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from entropy_mm.lifecycle import (
    AlreadyRunning,
    HealthState,
    LoopConfig,
    ProcessLock,
    install_signal_handlers,
    restore_signal_handlers,
    run_loop,
    write_health,
)

RUNTIME = ROOT / "runtime"


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=float(os.getenv("ENTROPY_CYCLE_INTERVAL_SECONDS", "2")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("ENTROPY_CYCLE_TIMEOUT_SECONDS", "20")))
    parser.add_argument("--max-failures", type=int, default=int(os.getenv("ENTROPY_MAX_CONSECUTIVE_FAILURES", "5")))
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument("--approval-min-cycles", type=int, default=int(os.getenv("ENTROPY_APPROVAL_MIN_CYCLES", "20")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if os.getenv("ENTROPY_LIVE_ENABLED", "false").lower() == "true":
        raise SystemExit("daemon live execution remains locked; use read-only mode")

    health_path = RUNTIME / "mm_health.json"
    cycle_path = RUNTIME / "mm_last_cycle.json"
    gate_path = RUNTIME / "mm_observation_gate.json"
    shutdown_path = RUNTIME / "mm_last_shutdown.json"
    stop_event = threading.Event()
    placeholder = HealthState()
    observed_cycles = 0
    previous = install_signal_handlers(stop_event, placeholder)

    def cycle() -> None:
        nonlocal observed_cycles
        result = subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/mm_dry_run.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-800:]
            raise RuntimeError(f"cycle exited {result.returncode}: {detail}")
        payload = json.loads(result.stdout)
        if payload.get("mode") != "read_only_dry_run":
            raise RuntimeError("unexpected cycle mode")
        observed_cycles += 1
        post = payload.get("post_execution", {})
        execution = post.get("execution", {})
        position = post.get("position", {})
        assessment = post.get("window_assessment", {})
        accepted_actions = sum(
            int(bool(item.get("accepted")))
            for key in ("cancel_results", "place_results")
            for item in execution.get(key, [])
        )
        ready = bool(
            observed_cycles >= args.approval_min_cycles
            and assessment.get("ready") is True
            and execution.get("mode") == "dry_run"
            and accepted_actions == 0
            and position.get("matched") is True
        )
        atomic_json(
            gate_path,
            {
                "ready_for_manual_approval": ready,
                "observed_cycles_this_run": observed_cycles,
                "required_cycles": args.approval_min_cycles,
                "window_metrics": post.get("window_metrics"),
                "window_assessment": assessment,
                "execution_status": execution.get("status"),
                "accepted_actions": accepted_actions,
                "position_matched": position.get("matched"),
                "signed_calls": 0,
            },
        )
        atomic_json(cycle_path, payload)

    def shutdown() -> None:
        result = subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/mm_shutdown.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-800:]
            raise RuntimeError(f"shutdown cleanup exited {result.returncode}: {detail}")
        payload = json.loads(result.stdout)
        if payload.get("mode") != "read_only_dry_run":
            raise RuntimeError("unexpected shutdown mode")
        atomic_json(shutdown_path, payload)

    try:
        with ProcessLock(RUNTIME / "mm_daemon.lock"):
            health = run_loop(
                cycle,
                shutdown,
                health_path=health_path,
                config=LoopConfig(
                    interval_seconds=args.interval,
                    max_consecutive_failures=args.max_failures,
                ),
                stop_event=stop_event,
                max_cycles=args.max_cycles,
            )
            if placeholder.stop_reason:
                health.stop_reason = placeholder.stop_reason
                write_health(health_path, health)
            return 0 if health.stop_reason != "failure_limit" else 1
    except AlreadyRunning as exc:
        now = __import__("time").time_ns() // 1_000_000
        write_health(
            health_path,
            HealthState(
                status="blocked",
                pid=os.getpid(),
                started_at_ms=now,
                updated_at_ms=now,
                last_error=str(exc),
                stop_reason="already_running",
            ),
        )
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        restore_signal_handlers(previous)


if __name__ == "__main__":
    raise SystemExit(main())
