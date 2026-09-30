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


def _verify_dryrun(opt, args) -> dict:
    """WO-1003-5 stage-2 gate: evaluate the dry-run database against the
    promotion criteria (Travis-approved 2026-09-30):
      1. staged params have run dry-run for >= window days
      2. >= N completed lifecycles (open+close cycles) — replaces the
         original trades>=10 which low-freq strategies never reach
      3. simulated net return >= -2% (catastrophe guard)
      4. max drawdown of the closed-PnL curve <= 15%
    All pass -> mark_dry_run_verified(note=criteria JSON). The command
    PRINTS the evaluation; marking is mechanical from the criteria —
    the human safety hop is the promote command itself."""
    import time as _t
    from src.state_db import DRYRUN_DB_PATH

    staged = opt._db.kv_get("optimized_params_staged")
    if not isinstance(staged, dict) or "params" not in staged:
        return {"status": "no_staged"}
    if staged.get("dry_run_verified"):
        return {"status": "verified", "already": True,
                "at": staged.get("dry_run_verified_at")}

    staged_age_s = _t.time() - float(staged.get("staged_at") or 0)
    criteria = {
        "window_days": {
            "required": args.dryrun_window_days,
            "actual": round(staged_age_s / 86400, 2),
            "pass": staged_age_s >= args.dryrun_window_days * 86400,
        }
    }

    import sqlite3
    dry = sqlite3.connect(f"file:{DRYRUN_DB_PATH}?mode=ro", uri=True)
    dry.row_factory = sqlite3.Row
    try:
        cutoff = staged.get("staged_at") or 0
        rows = dry.execute(
            "SELECT symbol, side, quantity, fill_price, fee_usdt, timestamp "
            "FROM paper_trades WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,)).fetchall()
    finally:
        dry.close()

    lifecycles = 0
    equity, peak, max_dd = 0.0, 0.0, 0.0
    books = {}  # symbol -> {qty, cost} average-cost open book
    for r in rows:
        sym, side = r["symbol"], r["side"]
        qty, px = float(r["quantity"]), float(r["fill_price"])
        fee = float(r["fee_usdt"] or 0)
        book = books.setdefault(sym, {"qty": 0.0, "cost": 0.0})
        if side == "BUY":
            book["qty"] += qty
            book["cost"] += qty * px + fee
        else:
            sell_qty = min(qty, book["qty"])
            if sell_qty > 0:
                avg = book["cost"] / book["qty"]
                carried = avg * sell_qty
                book["qty"] -= sell_qty
                book["cost"] -= carried
                equity += sell_qty * px - fee - carried
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)
            # completed lifecycle = symbol back to flat after a SELL
            if sell_qty > 0 and book["qty"] <= 1e-10:
                lifecycles += 1
    # net return vs the dry-run seed (400 USDT, WO-1003-5)
    seed = float(os.environ.get("DRYRUN_INITIAL_BALANCE", "400"))
    net_pct = 100.0 * equity / seed
    dd_pct = 100.0 * max_dd / seed

    criteria["lifecycles"] = {"required": args.min_lifecycles,
                              "actual": lifecycles,
                              "pass": lifecycles >= args.min_lifecycles}
    criteria["net_return_pct"] = {"threshold": -2.0, "actual": round(net_pct, 2),
                                  "pass": net_pct >= -2.0}
    criteria["max_drawdown_pct"] = {"threshold": 15.0, "actual": round(dd_pct, 2),
                                    "pass": dd_pct <= 15.0}

    all_pass = all(c["pass"] for c in criteria.values())
    out = {"status": "verified" if all_pass else "not_ready",
           "criteria": criteria, "marked": all_pass}
    if all_pass:
        opt.mark_dry_run_verified(note=json.dumps(criteria, default=str))
    return out


def _rollback_live_params(opt) -> dict:
    """WO-1003-5 rollback: restore the live key from the most recent
    'promoted' history snapshot. The rollback itself is recorded."""
    import time as _t
    hist = opt._db.kv_get("hyperopt:history") or []
    for entry in reversed(hist):
        if entry.get("event") == "promoted" and entry.get("old_params"):
            opt._db.kv_set("optimized_params", entry["old_params"])
            opt._append_history({
                "event": "rollback", "at": _t.time(),
                "restored_from_promoted_at": entry.get("at"),
                "params": entry["old_params"],
            })
            return {"status": "rolled_back",
                    "restored_from_promoted_at": entry.get("at"),
                    "params": entry["old_params"]}
    return {"status": "no_snapshot",
            "reason": "no promoted entry with old_params in history"}


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
    parser.add_argument("--verify-dryrun", action="store_true",
                        help="evaluate dryrun.db against the promotion "
                             "criteria and, if all pass, flag the staged "
                             "record dry-run verified (WO-1003-5 stage 2)")
    parser.add_argument("--dryrun-window-days", type=int, default=7,
                        help="minimum staged-age for --verify-dryrun "
                             "(default 7 days)")
    parser.add_argument("--min-lifecycles", type=int, default=3,
                        help="completed open+close cycles required "
                             "(Travis 2026-09-30: trades>=10 was "
                             "unreachable for this low-freq strategy)")
    parser.add_argument("--rollback", action="store_true",
                        help="restore live params from the last promoted "
                             "history snapshot (WO-1003-5 rollback path)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.verify_dryrun and args.rollback:
        print("--verify-dryrun and --rollback are exclusive", file=sys.stderr)
        return 2

    db = get_state_db()
    opt = ParamOptimizer(db=db)

    if args.verify_dryrun:
        result = _verify_dryrun(opt, args)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") in ("verified", "not_ready") else 1

    if args.rollback:
        result = _rollback_live_params(opt)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "rolled_back" else 1

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
