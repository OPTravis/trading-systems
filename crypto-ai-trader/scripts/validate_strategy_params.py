#!/usr/bin/env python3
"""
StrategyAdaptor Threshold Validator — run before any threshold change.

Walk-forward backtest to validate whether current parameters
are overfitted or degraded. Use this BEFORE adjusting StrategyAdaptor.

Exit code: 0 = healthy, 1 = degradation detected (do not change thresholds).

Usage:
    # Validate current params (run manually before adjustments)
    python scripts/validate_strategy_params.py

    # With specific symbol
    python scripts/validate_strategy_params.py --symbol SOL --days 180
"""
import sys
import os
import json
import logging
import argparse
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("src").setLevel(logging.WARNING)

from src.backtest import BacktestEngine
from src.binance_client import BinanceClient

# Validation thresholds
MIN_OOS_SHARPE = -1.0        # OOS Sharpe below this = degradation
MAX_OOS_DRAWDOWN = 20.0      # OOS drawdown above this = degradation
MIN_ROBUSTNESS = 33.0        # At least 1/3 splits should be positive

SYMBOLS = ["SOLUSDT", "ETHUSDT", "AVAXUSDT"]


def main():
    parser = argparse.ArgumentParser(description="Validate StrategyAdaptor parameters")
    parser.add_argument("--symbol", help="Single symbol to validate (default: all)")
    parser.add_argument("--days", type=int, default=180, help="Total backtest days (default: 180)")
    parser.add_argument("--splits", type=int, default=3, help="Walk-forward splits (default: 3)")
    args = parser.parse_args()

    client = BinanceClient(testnet=False)
    engine = BacktestEngine(binance_client=client, initial_capital=10000)

    symbols = [args.symbol.upper() + "USDT"] if args.symbol else SYMBOLS
    issues = []
    all_results = []

    for sym in symbols:
        sym_clean = sym.replace("USDT", "")
        print(f"\n--- Validating {sym_clean} ({args.days}d, {args.splits}-split walk-forward) ---")

        try:
            wf = engine.walk_forward(
                symbol=sym,
                interval="1h",
                total_days=args.days,
                train_pct=0.7,
                n_splits=args.splits,
                enable_trend_filter=False,
                enable_trailing_stop=True,
            )
        except Exception as e:
            print(f"  ❌ Walk-forward failed: {e}")
            issues.append(f"{sym_clean}: walk-forward failed — {e}")
            continue

        oos = wf.get("oos_summary", {})
        splits = wf.get("splits", [])

        avg_sharpe = oos.get("avg_sharpe", 0)
        avg_dd = oos.get("avg_max_drawdown_pct", 0)
        robustness = oos.get("robustness_pct", 0)
        avg_return = oos.get("avg_return_pct", 0)
        avg_wr = oos.get("avg_win_rate", 0)
        total_trades = oos.get("total_oos_trades", 0)

        status = "✅"
        if avg_sharpe < MIN_OOS_SHARPE:
            status = "🔴"
            issues.append(f"{sym_clean}: OOS Sharpe {avg_sharpe:.2f} < {MIN_OOS_SHARPE}")
        elif avg_dd > MAX_OOS_DRAWDOWN:
            status = "🟡"
            issues.append(f"{sym_clean}: OOS DD {avg_dd:.1f}% > {MAX_OOS_DRAWDOWN}%")
        elif robustness < MIN_ROBUSTNESS:
            status = "🟡"
            issues.append(f"{sym_clean}: Robustness {robustness:.0f}% < {MIN_ROBUSTNESS}%")

        print(f"  OOS Return: {avg_return:+.2f}% | Sharpe: {avg_sharpe:.2f} | "
              f"DD: {avg_dd:.1f}% | WinRate: {avg_wr:.0f}% | Robustness: {robustness:.0f}%")
        print(f"  Status: {status}")

        all_results.append({
            "symbol": sym_clean,
            "oos_return": avg_return,
            "oos_sharpe": avg_sharpe,
            "oos_dd": avg_dd,
            "robustness": robustness,
            "trades": total_trades,
        })

    # Summary
    print(f"\n{'='*50}")
    print("VALIDATION SUMMARY")
    print(f"{'='*50}")

    if issues:
        print(f"\n⚠️  {len(issues)} issue(s) detected — DO NOT change thresholds:")
        for issue in issues:
            print(f"  • {issue}")
        print(f"\n建議: 先跑完整 walk-forward 分析，確認策略參數是否需要調整")
        sys.exit(1)
    else:
        print(f"\n✅ All {len(all_results)} symbols passed validation.")
        print(f"   Current parameters are stable. Safe to adjust thresholds if needed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
