"""
Portfolio CLI - Command-line interface for portfolio management.

Delegates all business logic to PortfolioManager (src.portfolio).
Usage:
    python3 -m src.cli.portfolio --sync
    python3 -m src.cli.portfolio --list
    python3 -m src.cli.portfolio --add BTC 0.1 50000
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.binance_client import BinanceClient
from src.portfolio import PortfolioManager

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Portfolio Manager CLI")
    parser.add_argument(
        "--sync", action="store_true", help="Sync positions from Binance"
    )
    parser.add_argument("--list", action="store_true", help="List all positions")
    parser.add_argument("--summary", action="store_true", help="Show portfolio summary")
    parser.add_argument(
        "--add", nargs=3, metavar=("SYMBOL", "QTY", "PRICE"), help="Add position"
    )
    parser.add_argument("--remove", metavar="SYMBOL", help="Remove position")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    client = None
    try:
        client = BinanceClient()
        portfolio = PortfolioManager(config_path=None, binance_client=client)

        if args.sync:
            result = portfolio.sync_from_binance(client)
            print("✅ Synced" if result else "❌ Sync failed")

        elif args.list:
            positions = portfolio.get_all_positions()
            if not positions:
                print("No positions")
            for p in positions:
                pnl_val = p.get("pnl_value", p.get("unrealized_pnl", 0))
                pnl_pct = p.get("pnl_pct", p.get("unrealized_pct", 0))
                print(
                    f"{p['symbol']}: {p['quantity']} @ ${p['entry_price']:.4f} "
                    f"PnL: ${pnl_val:.2f} ({pnl_pct:.2f}%)"
                )

        elif args.summary:
            summary = portfolio.get_summary()
            print(json.dumps(summary, indent=2, default=str))

        elif args.add:
            symbol, qty, price = args.add
            portfolio.add_position(symbol, float(qty), float(price))
            pos = portfolio.get_position(symbol)
            print(
                json.dumps(
                    {
                        "success": True,
                        "symbol": symbol,
                        "quantity": pos["quantity"] if pos else 0,
                        "entry_price": pos["entry_price"] if pos else 0,
                    },
                    indent=2,
                    default=str,
                )
            )

        elif args.remove:
            result = portfolio.close_position(args.remove)
            if result:
                print(
                    json.dumps(
                        {
                            "success": True,
                            "symbol": args.remove,
                            "pnl": result.get("pnl", 0),
                        },
                        indent=2,
                        default=str,
                    )
                )
            else:
                print(json.dumps({"error": f"No position for {args.remove}"}, indent=2))

        else:
            parser.print_help()

    finally:
        if client:
            client.close()


if __name__ == "__main__":
    main()
