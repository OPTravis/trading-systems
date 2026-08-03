#!/usr/bin/env python3
"""
Walk-Forward Parameter Optimization

Runs walk-forward validation with different parameter configurations
to find the best setup for each symbol.

Usage:
    python3 scripts/wf_optimize.py ETH AVAX --days 180 --wf-splits 5
    python3 scripts/wf_optimize.py ETH --days 180 --wf-splits 5 --json
"""

import argparse
import json
import os
import sys
import time
import copy
from typing import Dict, List, Any

sys.path.insert(0, os.path.expanduser("~/trading-systems/crypto-ai-trader"))

from src.backtest import BacktestEngine
from src.binance_client import BinanceClient


# ─── Parameter Configurations ───────────────────────────────────────

ETH_CONFIGS = [
    {
        "name": "A_baseline",
        "desc": "Current defaults (no trend/trailing)",
        "sl_atr": 2.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 50, "max_sl_pct": 12.0, "trend": False, "trailing": False,
    },
    {
        "name": "B_report_rec",
        "desc": "Report recommendation: tighter SL, higher score, trend+trailing",
        "sl_atr": 1.5, "tp1": 2.5, "tp2": 4.5, "tp3": 7.0,
        "score": 60, "max_sl_pct": 8.0, "trend": True, "trailing": True,
    },
    {
        "name": "C_conservative",
        "desc": "Conservative: tight SL, high score threshold",
        "sl_atr": 1.5, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 65, "max_sl_pct": 8.0, "trend": True, "trailing": True,
    },
    {
        "name": "D_wider_tp",
        "desc": "Wider TP targets, moderate score",
        "sl_atr": 2.0, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 55, "max_sl_pct": 10.0, "trend": True, "trailing": True,
    },
    {
        "name": "E_tight_sl_wide_tp",
        "desc": "Tight SL + wide TP (better risk-reward)",
        "sl_atr": 1.5, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 60, "max_sl_pct": 8.0, "trend": True, "trailing": True,
    },
    {
        "name": "F_trend_only",
        "desc": "Just add trend filter to baseline",
        "sl_atr": 2.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 50, "max_sl_pct": 12.0, "trend": True, "trailing": False,
    },
]

AVAX_CONFIGS = [
    {
        "name": "A_baseline",
        "desc": "Current defaults (no trend/trailing)",
        "sl_atr": 2.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 50, "max_sl_pct": 12.0, "trend": False, "trailing": False,
    },
    {
        "name": "B_report_rec",
        "desc": "Report recommendation: wider TP, trend+trailing",
        "sl_atr": 2.5, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 55, "max_sl_pct": 15.0, "trend": True, "trailing": True,
    },
    {
        "name": "C_wider_sl",
        "desc": "Even wider SL for high volatility",
        "sl_atr": 3.0, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 55, "max_sl_pct": 15.0, "trend": True, "trailing": True,
    },
    {
        "name": "D_tight_score",
        "desc": "Higher score threshold to reduce noise",
        "sl_atr": 2.5, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 60, "max_sl_pct": 12.0, "trend": True, "trailing": True,
    },
    {
        "name": "E_aggressive_tp",
        "desc": "Very wide TP for AVAX volatility",
        "sl_atr": 2.5, "tp1": 3.5, "tp2": 6.0, "tp3": 10.0,
        "score": 55, "max_sl_pct": 15.0, "trend": True, "trailing": True,
    },
    {
        "name": "F_trend_only",
        "desc": "Just add trend filter to baseline",
        "sl_atr": 2.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 50, "max_sl_pct": 12.0, "trend": True, "trailing": False,
    },
]

LINK_CONFIGS = [
    {
        "name": "A_baseline",
        "desc": "Current defaults (no trend/trailing)",
        "sl_atr": 2.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 50, "max_sl_pct": 12.0, "trend": False, "trailing": False,
    },
    {
        "name": "B_high_threshold",
        "desc": "Higher score threshold to reduce frequency",
        "sl_atr": 2.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 70, "max_sl_pct": 10.0, "trend": True, "trailing": True,
    },
    {
        "name": "C_wider_tp",
        "desc": "Wider TP to improve profit factor",
        "sl_atr": 1.5, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 65, "max_sl_pct": 8.0, "trend": True, "trailing": True,
    },
    {
        "name": "D_trailing_focus",
        "desc": "Tight SL + trailing to cap losses",
        "sl_atr": 1.5, "tp1": 2.5, "tp2": 4.5, "tp3": 7.0,
        "score": 65, "max_sl_pct": 8.0, "trend": True, "trailing": True,
    },
]

SOL_CONFIGS = [
    {
        "name": "A_baseline",
        "desc": "Current defaults (no trend/trailing)",
        "sl_atr": 2.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 50, "max_sl_pct": 12.0, "trend": False, "trailing": False,
    },
    {
        "name": "B_wide_sl",
        "desc": "Wider SL for SOL's high volatility",
        "sl_atr": 3.0, "tp1": 2.0, "tp2": 4.0, "tp3": 6.0,
        "score": 50, "max_sl_pct": 15.0, "trend": True, "trailing": True,
    },
    {
        "name": "C_tight_score",
        "desc": "High score threshold + trend filter",
        "sl_atr": 2.5, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 65, "max_sl_pct": 10.0, "trend": True, "trailing": True,
    },
    {
        "name": "D_conservative",
        "desc": "Very conservative: minimal trades, tight risk",
        "sl_atr": 1.5, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0,
        "score": 70, "max_sl_pct": 7.0, "trend": True, "trailing": True,
    },
]

SYMBOL_CONFIGS = {
    "ETH": ETH_CONFIGS,
    "ETHUSDT": ETH_CONFIGS,
    "AVAX": AVAX_CONFIGS,
    "AVAXUSDT": AVAX_CONFIGS,
    "LINK": LINK_CONFIGS,
    "LINKUSDT": LINK_CONFIGS,
    "SOL": SOL_CONFIGS,
    "SOLUSDT": SOL_CONFIGS,
}


def apply_config(engine: BacktestEngine, config: dict):
    """Patch BacktestEngine class constants with config values."""
    engine.SL_ATR_MULT = config["sl_atr"]
    engine.TP1_ATR_MULT = config["tp1"]
    engine.TP2_ATR_MULT = config["tp2"]
    engine.TP3_ATR_MULT = config["tp3"]
    engine.SCORE_THRESHOLD = config["score"]
    engine.MAX_SL_PCT = config["max_sl_pct"]


def run_config(
    client: BinanceClient,
    symbol: str,
    config: dict,
    days: int,
    wf_splits: int,
    wf_train: float,
    capital: float,
) -> Dict[str, Any]:
    """Run walk-forward with a specific config."""
    engine = BacktestEngine(binance_client=client, initial_capital=capital)
    apply_config(engine, config)

    t0 = time.time()
    result = engine.walk_forward(
        symbol=symbol,
        interval="1h",
        total_days=days,
        train_pct=wf_train,
        n_splits=wf_splits,
        enable_trend_filter=config["trend"],
        enable_trailing_stop=config["trailing"],
    )
    elapsed = time.time() - t0

    oos = result.get("oos_summary", {})
    return {
        "config_name": config["name"],
        "config_desc": config["desc"],
        "params": {
            "sl_atr": config["sl_atr"],
            "tp1": config["tp1"],
            "tp2": config["tp2"],
            "tp3": config["tp3"],
            "score": config["score"],
            "max_sl_pct": config["max_sl_pct"],
            "trend": config["trend"],
            "trailing": config["trailing"],
        },
        "oos_avg_return_pct": oos.get("avg_return_pct", 0),
        "oos_avg_sharpe": oos.get("avg_sharpe", 0),
        "oos_avg_max_dd_pct": oos.get("avg_max_drawdown_pct", 0),
        "oos_avg_win_rate": oos.get("avg_win_rate", 0),
        "oos_avg_pf": oos.get("avg_profit_factor", 0),
        "oos_total_trades": oos.get("total_trades", 0),
        "oos_positive_splits": oos.get("positive_splits", 0),
        "oos_total_splits": oos.get("total_splits", 0),
        "robustness_pct": oos.get("robustness_pct", 0),
        "elapsed_seconds": round(elapsed, 1),
        "splits": result.get("splits", []),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Walk-forward parameter optimization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("symbols", nargs="+", help="Symbols to optimize (ETH AVAX LINK SOL)")
    parser.add_argument("--days", type=int, default=180, help="Backtest period in days")
    parser.add_argument("--wf-splits", type=int, default=5, help="Walk-forward splits (default: 5)")
    parser.add_argument("--wf-train", type=float, default=0.7, help="Train ratio (default: 0.7)")
    parser.add_argument("--capital", type=float, default=10000, help="Initial capital")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    client = BinanceClient(testnet=False)

    all_results = {}

    for sym_input in args.symbols:
        sym = sym_input.upper()
        if not sym.endswith("USDT"):
            sym = sym + "USDT"
        sym_key = sym.replace("USDT", "")

        configs = SYMBOL_CONFIGS.get(sym) or SYMBOL_CONFIGS.get(sym_key)
        if not configs:
            print(f"⚠️  No configs defined for {sym}, skipping.", file=sys.stderr)
            continue

        print(f"\n{'═' * 65}", file=sys.stderr)
        print(f"  Optimizing {sym} — {len(configs)} configs × {args.wf_splits} splits", file=sys.stderr)
        print(f"{'═' * 65}", file=sys.stderr)

        sym_results = []
        for i, cfg in enumerate(configs):
            print(f"\n  [{i+1}/{len(configs)}] {cfg['name']}: {cfg['desc']}", file=sys.stderr)
            print(f"    SL={cfg['sl_atr']} TP={cfg['tp1']}/{cfg['tp2']}/{cfg['tp3']} "
                  f"Score={cfg['score']} MaxSL={cfg['max_sl_pct']}% "
                  f"Trend={'Y' if cfg['trend'] else 'N'} Trail={'Y' if cfg['trailing'] else 'N'}",
                  file=sys.stderr)

            try:
                r = run_config(
                    client, sym, cfg,
                    days=args.days, wf_splits=args.wf_splits,
                    wf_train=args.wf_train, capital=args.capital,
                )
                sym_results.append(r)
                print(f"    → OOS: {r['oos_avg_return_pct']:+.2f}% | "
                      f"PF={r['oos_avg_pf']:.2f} | Sharpe={r['oos_avg_sharpe']:.2f} | "
                      f"WR={r['oos_avg_win_rate']:.1f}% | DD={r['oos_avg_max_dd_pct']:.1f}% | "
                      f"Trades={r['oos_total_trades']} | "
                      f"Robust={r['robustness_pct']:.0f}% | "
                      f"{r['elapsed_seconds']:.0f}s",
                      file=sys.stderr)
            except Exception as e:
                print(f"    ❌ Error: {e}", file=sys.stderr)
                sym_results.append({
                    "config_name": cfg["name"],
                    "config_desc": cfg["desc"],
                    "error": str(e),
                })

        # Sort by OOS return (best first)
        valid = [r for r in sym_results if "error" not in r]
        valid.sort(key=lambda x: x["oos_avg_return_pct"], reverse=True)
        all_results[sym] = {
            "configs_tested": len(configs),
            "configs_valid": len(valid),
            "best_config": valid[0] if valid else None,
            "all_results": sym_results,
            "sorted_by_return": valid,
        }

        if valid:
            best = valid[0]
            print(f"\n  ✅ Best for {sym}: {best['config_name']}", file=sys.stderr)
            print(f"     OOS Return: {best['oos_avg_return_pct']:+.2f}% | "
                  f"PF={best['oos_avg_pf']:.2f} | Sharpe={best['oos_avg_sharpe']:.2f}",
                  file=sys.stderr)

    if args.json:
        print(json.dumps(all_results, indent=2, default=str))
    else:
        _print_summary(all_results)

    # Also write full results to file
    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "wf_optimization_results.json"
    )
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n📁 Full results saved to: {output_path}", file=sys.stderr)


def _print_summary(all_results: Dict):
    """Print human-readable summary."""
    print(f"\n{'═' * 70}")
    print(f"  WALK-FORWARD PARAMETER OPTIMIZATION SUMMARY".center(70))
    print(f"{'═' * 70}")

    for sym, data in all_results.items():
        print(f"\n┌─ {sym} ({data['configs_valid']}/{data['configs_tested']} configs valid)")
        print(f"│")

        sorted_results = data.get("sorted_by_return", [])
        if not sorted_results:
            print(f"│  ❌ No valid results")
            continue

        best = sorted_results[0]
        print(f"│  🏆 Best: {best['config_name']} — {best['config_desc']}")
        p = best["params"]
        print(f"│     SL={p['sl_atr']} TP={p['tp1']}/{p['tp2']}/{p['tp3']} "
              f"Score={p['score']} MaxSL={p['max_sl_pct']}% "
              f"Trend={'Y' if p['trend'] else 'N'} Trail={'Y' if p['trailing'] else 'N'}")
        print(f"│     OOS Return: {best['oos_avg_return_pct']:+.2f}% | "
              f"PF={best['oos_avg_pf']:.2f} | Sharpe={best['oos_avg_sharpe']:.2f} | "
              f"WR={best['oos_avg_win_rate']:.1f}% | DD={best['oos_avg_max_dd_pct']:.1f}%")
        print(f"│")

        print(f"│  {'Config':<20} {'OOS Ret':>8} {'PF':>6} {'Sharpe':>7} {'WR':>6} {'DD':>7} {'Trades':>7} {'Robust':>7}")
        print(f"│  {'─' * 72}")
        for r in sorted_results:
            print(f"│  {r['config_name']:<20} {r['oos_avg_return_pct']:>+7.2f}% "
                  f"{r['oos_avg_pf']:>6.2f} {r['oos_avg_sharpe']:>7.2f} "
                  f"{r['oos_avg_win_rate']:>5.1f}% {r['oos_avg_max_dd_pct']:>6.1f}% "
                  f"{r['oos_total_trades']:>7} {r['robustness_pct']:>6.0f}%")

        print(f"└{'─' * 73}")

    print(f"\n{'═' * 70}")


if __name__ == "__main__":
    main()
