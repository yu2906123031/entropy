#!/usr/bin/env python3
"""Replay recorded JSONL order books through the adaptive quote engine."""
from __future__ import annotations

from dataclasses import asdict
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from entropy_mm.replay import load_jsonl, replay_quotes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="JSONL with time_ms and Hyperliquid-style levels")
    parser.add_argument("--inventory", type=float, default=0.0)
    parser.add_argument("--max-inventory", type=float, default=1.0)
    parser.add_argument("--base-half-spread-bps", type=float, default=35.0)
    parser.add_argument("--fee-bps", type=float, default=3.0)
    parser.add_argument("--minimum-profit-bps", type=float, default=8.0)
    args = parser.parse_args()
    snapshots = load_jsonl(args.path)
    result = replay_quotes(
        snapshots,
        inventory=args.inventory,
        max_inventory=args.max_inventory,
        base_half_spread_bps=args.base_half_spread_bps,
        fee_bps=args.fee_bps,
        minimum_profit_bps=args.minimum_profit_bps,
    )
    print(json.dumps({"mode": "read_only_shadow_replay", "input": args.path, "result": asdict(result)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
