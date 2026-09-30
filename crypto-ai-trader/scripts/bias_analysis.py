#!/usr/bin/env python3
"""WO-1001-3: pre-launch bias gate CLI.

Usage:
  python3 scripts/bias_analysis.py                      # all strategies
  python3 scripts/bias_analysis.py --strategy BollingerStrategy
  python3 scripts/bias_analysis.py --params '{"period":30}'   # new param set

Exit code 0 = all PASS (safe to launch), 1 = bias detected, 2 = usage error.
Run this before promoting ANY new strategy or new parameter set to live.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bias_analysis import discover_strategies, run_bias_gate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="lookahead/repaint bias gate")
    ap.add_argument("--strategy", help="class name (default: all)")
    ap.add_argument("--params", help="JSON strategy params for the gate")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    params = None
    if args.params:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as exc:
            print(f"usage error: --params not valid JSON ({exc})")
            return 2

    cls = None
    if args.strategy:
        found = {c.__name__: c for c in discover_strategies()}
        if args.strategy not in found:
            print(f"usage error: unknown strategy {args.strategy}; "
                  f"known: {sorted(found)}")
            return 2
        cls = found[args.strategy]

    reports = run_bias_gate(strategy_cls=cls, params=params)
    failed = False
    for r in reports:
        print(r.summary())
        for e in r.examples[:5]:
            print(f"    {e}")
        failed = failed or not r.ok
    print(f"\nBIAS GATE: {'FAIL — do NOT promote to live' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
