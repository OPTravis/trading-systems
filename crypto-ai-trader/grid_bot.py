#!/usr/bin/env python3
"""
Grid Trading Bot — CLI Entry Point

Usage:
    python grid_bot.py init --symbol SOLUSDT --capital 400 --grids 8 --range 5
    python grid_bot.py start [--dry-run]
    python grid_bot.py stop
    python grid_bot.py pause
    python grid_bot.py status
    python grid_bot.py tick
    python grid_bot.py backtest --symbol SOLUSDT --capital 400 --grids 8 --range 5 --days 30
"""

import argparse
import json
import sys
import os

# Python 3.11.15 (uv build) removed random.randbits
import random as _r
if not hasattr(_r, 'randbits'):
    _r.randbits = _r.getrandbits

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.binance_client import BinanceClient
from src.grid_trader import GridBot


def main():
    parser = argparse.ArgumentParser(description="Binance Spot Grid Trading Bot")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init
    p_init = subparsers.add_parser("init", help="Initialize grid configuration")
    p_init.add_argument("--symbol", required=True, help="Trading pair (e.g., SOLUSDT)")
    p_init.add_argument("--capital", type=float, required=True, help="Total capital in USDT")
    p_init.add_argument("--grids", type=int, default=8, help="Number of grid levels (default: 8)")
    p_init.add_argument("--range", type=float, default=5.0, dest="range_pct", help="Price range %% (default: 5)")
    p_init.add_argument("--rebalance-hours", type=int, default=24, help="Rebalance interval in hours (default: 24)")
    p_init.add_argument("--max-range", type=float, default=15.0, help="Max range deviation before rebalance (default: 15%%)")

    # start
    p_start = subparsers.add_parser("start", help="Start grid trading (place orders)")
    p_start.add_argument("--dry-run", action="store_true", help="Simulate without real orders")

    # stop
    subparsers.add_parser("stop", help="Stop grid trading (cancel all orders)")

    # pause
    subparsers.add_parser("pause", help="Pause grid trading (keep state)")

    # status
    subparsers.add_parser("status", help="Show grid status")

    # tick
    subparsers.add_parser("tick", help="Run one check cycle (for cron)")

    # backtest
    p_bt = subparsers.add_parser("backtest", help="Run historical backtest")
    p_bt.add_argument("--symbol", required=True)
    p_bt.add_argument("--capital", type=float, default=400)
    p_bt.add_argument("--grids", type=int, default=8)
    p_bt.add_argument("--range", type=float, default=5.0, dest="range_pct")
    p_bt.add_argument("--days", type=int, default=30)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    client = BinanceClient()
    bot = GridBot(client)

    if args.command == "init":
        result = bot.init_grid(
            symbol=args.symbol,
            total_capital=args.capital,
            grid_count=args.grids,
            range_pct=args.range_pct,
            rebalance_interval_hours=args.rebalance_hours,
            max_range_pct=args.max_range,
        )
        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)
        print(json.dumps(result, indent=2))

    elif args.command == "start":
        result = bot.start(dry_run=args.dry_run)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "stop":
        result = bot.stop()
        print(json.dumps(result, indent=2))

    elif args.command == "pause":
        result = bot.pause()
        print(json.dumps(result, indent=2))

    elif args.command == "status":
        result = bot.get_status()
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "tick":
        result = bot.tick()
        # One-line summary for cron
        if "error" in result:
            print(f"GRID ERROR: {result['error']}")
        elif result.get("status") == "skip":
            pass  # silent
        else:
            sym = result.get("symbol", "?")
            price = result.get("current_price", 0)
            fills = result.get("fills_processed", 0)
            trades = result.get("total_trades", 0)
            pnl = result.get("realized_pnl", 0)
            eq = result.get("equity", 0)
            rebal = " REBALANCED" if result.get("rebalanced") else ""
            print(f"GRID {sym}: ${price:.4f} fills={fills} trades={trades} pnl=${pnl:.2f} equity=${eq:.2f}{rebal}")

    elif args.command == "backtest":
        result = bot.backtest(
            symbol=args.symbol,
            total_capital=args.capital,
            grid_count=args.grids,
            range_pct=args.range_pct,
            days=args.days,
        )
        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)
        print(json.dumps(result, indent=2))

    client.close()


if __name__ == "__main__":
    main()
