#!/usr/bin/env python3
"""WO-1003-6: Bayesian (TPE) hyperopt CLI with robust loss functions.

Usage:
    python3 scripts/hyperopt.py --loss sharpe_daily --trials 40
    python3 scripts/hyperopt.py --loss max_drawdown --symbols SOL ETH --json
    python3 scripts/hyperopt.py --promote            # gated: wf + dry-run
    python3 scripts/hyperopt.py --promote --force    # human-approved only

Pipeline: TPE search -> walk-forward validation -> STAGED (never live).
Promotion requires the walk-forward gate AND the dry-run gate
(WO-1003-5 harness calls mark_dry_run_verified); --force bypasses the
dry-run gate only, never the walk-forward gate.
Exit codes: 0 ok, 1 optimisation/promotion failed, 2 usage error.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/trading-systems/crypto-ai-trader"))

from src.hyperopt_loss import LOSS_FUNCTIONS
from src.param_optimizer import ParamOptimizer
from src.state_db import get_state_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loss", default="sharpe_daily",
                        choices=sorted(LOSS_FUNCTIONS))
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--promote", action="store_true",
                        help="promote staged params (gated)")
    parser.add_argument("--force", action="store_true",
                        help="with --promote: skip dry-run gate (never skips "
                             "walk-forward gate)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    db = get_state_db()
    opt = ParamOptimizer(db=db)

    if args.promote:
        result = opt.promote_staged_params(force=args.force)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "promoted" else 1

    result = opt.bayesian_optimize(
        symbols=args.symbols, loss=args.loss, n_trials=args.trials,
        days=args.days, seed=args.seed,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        if result.get("status") != "staged":
            print(f"❌ {result.get('status')}: {result.get('reason', '')}")
            return 1
        v = result["validation"]
        print(f"✅ staged — loss={result['loss_name']} "
              f"{result['loss_value']:.4f} over {result['n_trials']} trials")
        print(f"   wf_validated={v.get('validated')} "
              f"({v.get('reason', '')})")
        print(f"   params: {json.dumps(result['params'], default=str)}")
        print("   promote with: python3 scripts/hyperopt.py --promote "
              "(gated: walk-forward + dry-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
