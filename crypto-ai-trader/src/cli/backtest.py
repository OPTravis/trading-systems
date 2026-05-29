"""
CLI entry point for crypto-ai-trader backtest engine.

This module provides the command-line interface for running backtests.
It delegates all trading logic to the BacktestEngine class.

Usage:
    python3 -m src.cli.backtest --symbol BTC --days 90
    python3 -m src.cli.backtest --symbols BTC,ETH,SOL --days 90 --trend-filter
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest import BacktestEngine


def main():
    parser = argparse.ArgumentParser(
        description="Crypto AI Trader — Backtest Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m src.cli.backtest --symbol BTC --days 90
  python3 -m src.cli.backtest --symbol ETH --days 180 --trend-filter --trailing-stop
  python3 -m src.cli.backtest --symbols BTC,ETH,SOL --days 90
  python3 -m src.cli.backtest --symbol BTC --start 2025-01-01 --end 2025-06-01
        """,
    )
    parser.add_argument("--symbol", "-s", type=str, help="Single symbol to backtest (e.g. BTC or BTCUSDT)")
    parser.add_argument("--symbols", type=str, help="Multiple symbols, comma-separated (e.g. BTC,ETH,SOL)")
    parser.add_argument("--interval", type=str, default="1h", choices=["1m","5m","15m","30m","1h","4h","1d"], help="K-line interval (default: 1h)")
    parser.add_argument("--days", type=int, default=90, help="Number of days to backtest (default: 90)")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000, help="Initial capital in USDT (default: 10000)")
    parser.add_argument("--trend-filter", action="store_true", help="Enable BTC 200MA trend filter")
    parser.add_argument("--trailing-stop", action="store_true", help="Enable trailing stop-loss")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.symbol and not args.symbols:
        parser.error("Either --symbol or --symbols is required")
        return

    # Initialize BinanceClient
    try:
        from src.binance_client import BinanceClient
        client = BinanceClient()
    except Exception as e:
        print(f"ERROR: Failed to initialize BinanceClient: {e}")
        print("Make sure BINANCE_API_KEY and BINANCE_API_SECRET are set.")
        return

    try:
        engine = BacktestEngine(client, initial_capital=args.capital)

        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",")]
            result = engine.run_multi(
                symbols=symbols,
                interval=args.interval,
                start_date=args.start,
                end_date=args.end,
                days=args.days,
                enable_trend_filter=args.trend_filter,
                enable_trailing_stop=args.trailing_stop,
            )
            report = BacktestEngine.generate_report(result.get("summary", {}))
            print(report)

            # Also print individual results
            for sym, sym_result in result.get("individual", {}).items():
                print()
                print(BacktestEngine.generate_report(sym_result))

        else:
            result = engine.run(
                symbol=args.symbol,
                interval=args.interval,
                start_date=args.start,
                end_date=args.end,
                days=args.days,
                enable_trend_filter=args.trend_filter,
                enable_trailing_stop=args.trailing_stop,
            )
            report = BacktestEngine.generate_report(result)
            print(report)

    finally:
        client.close()


if __name__ == "__main__":
    main()
