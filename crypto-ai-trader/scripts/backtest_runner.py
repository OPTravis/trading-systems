#!/usr/bin/env python3
"""
Backtest Runner CLI — run backtests, walk-forward analysis, and multi-symbol comparison.

Usage:
    # Single symbol backtest (90 days, 1h, with trend + trailing)
    python scripts/backtest_runner.py SOL --trend --trailing --days 90

    # Walk-forward (180 days, 3 splits, 70/30 train/test)
    python scripts/backtest_runner.py SOL --walk-forward --days 180

    # Multi-symbol comparison
    python scripts/backtest_runner.py SOL ETH AVAX OP --days 90 --trend --trailing

    # Output JSON
    python scripts/backtest_runner.py SOL --json
"""

import argparse
import json
import logging
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))

from src.backtest import BacktestEngine
from src.binance_client import BinanceClient


def main():
    parser = argparse.ArgumentParser(
        description="Crypto backtest runner with walk-forward analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "symbols", nargs="+", help="Trading symbols (e.g. SOL ETH AVAX)"
    )
    parser.add_argument("--days", type=int, default=90, help="Backtest period in days (default: 90)")
    parser.add_argument("--interval", default="1h", help="Kline interval (default: 1h)")
    parser.add_argument("--capital", type=float, default=10000, help="Initial capital in USDT (default: 10000)")
    parser.add_argument("--trend", action="store_true", help="Enable BTC trend filter")
    parser.add_argument("--trailing", action="store_true", help="Enable trailing stop")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward analysis")
    parser.add_argument("--wf-splits", type=int, default=3, help="Walk-forward splits (default: 3)")
    parser.add_argument("--wf-train", type=float, default=0.7, help="Walk-forward train ratio (default: 0.7)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of formatted report")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    # Setup logging
    level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Suppress noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("src").setLevel(logging.WARNING)

    client = BinanceClient(testnet=False)
    engine = BacktestEngine(binance_client=client, initial_capital=args.capital)

    symbols = [s.upper() for s in args.symbols]
    # Standardize: add USDT if not present
    symbols = [s if s.endswith("USDT") else s + "USDT" for s in symbols]

    if args.walk_forward:
        # Walk-forward analysis
        if len(symbols) > 1:
            print("⚠️  Walk-forward supports one symbol at a time. Using first symbol.", file=sys.stderr)
            symbols = symbols[:1]

        result = engine.walk_forward(
            symbol=symbols[0],
            interval=args.interval,
            total_days=args.days,
            train_pct=args.wf_train,
            n_splits=args.wf_splits,
            enable_trend_filter=args.trend,
            enable_trailing_stop=args.trailing,
        )

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_walk_forward(result)

    elif len(symbols) == 1:
        # Single symbol backtest
        result = engine.run(
            symbol=symbols[0],
            interval=args.interval,
            days=args.days,
            enable_trend_filter=args.trend,
            enable_trailing_stop=args.trailing,
        )

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(BacktestEngine.generate_report(result))

    else:
        # Multi-symbol comparison
        result = engine.run_multi(
            symbols=symbols,
            interval=args.interval,
            days=args.days,
            enable_trend_filter=args.trend,
            enable_trailing_stop=args.trailing,
        )

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            summary = result.get("summary", {})
            print(f"\n{'═' * 60}")
            print(f"  MULTI-SYMBOL BACKTEST".center(60))
            print(f"{'═' * 60}")
            print(f"  Symbols:     {', '.join(symbols)}")
            print(f"  Period:      {args.days} days ({args.interval})")
            print(f"  Capital:     ${args.capital:,.2f}")
            print(f"  Trend:       {'ON' if args.trend else 'OFF'}")
            print(f"  Trailing:    {'ON' if args.trailing else 'OFF'}")
            print()
            print(f"  Total Return:    {summary.get('total_return_pct', 0):+.2f}%")
            print(f"  Win Rate:        {summary.get('win_rate', 0):.1f}%")
            print(f"  Profit Factor:   {summary.get('profit_factor', 0)}")
            print(f"  Total Trades:    {summary.get('total_trades', 0)}")
            print()
            print(f"  {'Symbol':<12} {'Return':>8} {'Trades':>7} {'WinRate':>8} {'MaxDD':>8}")
            print(f"  {'─' * 47}")
            for sym, s in summary.get("per_symbol", {}).items():
                print(
                    f"  {sym:<12} {s['total_return_pct']:>+7.2f}% "
                    f"{s['total_trades']:>7} {s['win_rate']:>7.1f}% "
                    f"{s['max_drawdown_pct']:>7.2f}%"
                )
            print(f"{'═' * 60}")


def _print_walk_forward(result: Dict):
    """Print walk-forward results in human-readable format."""
    splits = result.get("splits", [])
    oos = result.get("oos_summary", {})

    print(f"\n{'═' * 65}")
    print(f"  WALK-FORWARD ANALYSIS: {result['symbol']}".center(65))
    print(f"{'═' * 65}")
    print(f"  Total Period:  {result['total_days']} days")
    print(f"  Splits:        {result['n_splits']}")
    print(f"  Train/Test:    {result['train_pct']:.0%}/{1-result['train_pct']:.0%}")
    print()
    print(f"  {'#':<4} {'Train Return':>13} {'OOS Return':>12} {'OOS Sharpe':>12} {'OOS WinRate':>13} {'OOS MaxDD':>10}")
    print(f"  {'─' * 61}")
    for s in splits:
        tr = s["train"]
        te = s["test"]
        print(
            f"  {s['split']:<4} "
            f"{tr['return_pct']:>+12.2f}% "
            f"{te['return_pct']:>+11.2f}% "
            f"{te['sharpe']:>11.2f} "
            f"{te['win_rate']:>12.1f}% "
            f"{te['max_dd']:>9.2f}%"
        )
    print()
    print(f"  {'─' * 61}")
    print(f"  OOS Summary (Out-of-Sample)")
    print(f"  {'─' * 61}")
    print(f"  Avg Return:      {oos.get('avg_return_pct', 0):+.2f}%")
    print(f"  Avg Sharpe:      {oos.get('avg_sharpe', 0):.2f}")
    print(f"  Avg Max DD:      {oos.get('avg_max_drawdown_pct', 0):.2f}%")
    print(f"  Avg Win Rate:    {oos.get('avg_win_rate', 0):.1f}%")
    print(f"  Total OOS Trades:{oos.get('total_oos_trades', 0)}")
    print(f"  Robustness:      {oos.get('robustness_pct', 0):.0f}%")
    print(f"{'═' * 65}")


if __name__ == "__main__":
    main()
