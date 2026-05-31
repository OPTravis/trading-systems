"""
Funding Rate Arbitrage — Delta Neutral Strategy (SPOT + FUTURES)

⚠️  WARNING: This module requires Binance FUTURES API.
    Currently SPOT ONLY system — this module is DISABLED.

Strategy: Earn funding rate payments by holding spot long + perp short.
Zero directional risk (delta neutral), profit from funding rate.

Requirements:
- Binance Futures API enabled (NOT CURRENTLY AVAILABLE)
- Sufficient capital (recommend $500+ per position)
- Monitor liquidation risk on the short leg

Usage (when futures enabled):
    from src.funding_arb import FundingArbitrage
    arb = FundingArbitrage(spot_client, futures_client)
    opportunities = arb.scan_opportunities()   # Scan only — safe
    arb.open_position(symbol, capital_usdt)    # Needs futures client
    arb.check_positions()
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from src.strategy_guard import strategy_guard
from src.utils import get_project_root

_DATA_DIR = get_project_root() / "data" / "funding_arb"


class FundingArbitrage:
    """Delta-neutral funding rate arbitrage.

    Strategy:
    1. Find coins with persistently high positive funding rates
    2. Buy spot (long) + Short perp (equal notional)
    3. Collect funding payments every 8h
    4. Close when funding rate drops or position reaches target profit

    Risk management:
    - Max single position: 30% of capital
    - Stop loss: -2% from entry (basis divergence)
    - Close if funding rate reverses negative for 3 consecutive periods
    """

    # Minimum funding rate to open position (annualized ~15%+)
    MIN_FUNDING_RATE = 0.0003  # 0.03% per 8h = ~33% annualized

    # Maximum basis (spot premium over perp) — high basis = expensive entry
    MAX_BASIS_PCT = 1.0  # 1%

    def __init__(self, spot_client, futures_client=None):
        self.spot = spot_client
        self.futures = futures_client
        self._data_dir = _DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._data_dir / "positions.json"
        self._state = self._load_state()

    def _load_state(self) -> Dict:
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            state = db.dca_get("funding_arb")  # reuse dca table for key-value
            if state and isinstance(state, dict):
                return state
        except Exception:
            logger.error("Failed to load funding arb state from StateDB", exc_info=True)
        # Fallback: try legacy JSON once
        try:
            if self._state_file.exists():
                with open(self._state_file, "r") as f:
                    return json.load(f)
        except Exception:
            logger.error(
                "Failed to load funding arb state from legacy JSON file", exc_info=True
            )
        return {"positions": {}, "history": []}

    def _save_state(self):
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            db.dca_set("funding_arb", self._state)
        except Exception as e:
            logger.error("Failed to save funding arb state: %s", e)

    @strategy_guard(max_failures=2, cooldown_sec=300, default_return=[])
    def scan_opportunities(self, symbols: Optional[List[str]] = None) -> List[Dict]:
        """Scan for funding rate arbitrage opportunities.

        ⚠️  Uses public futures API for data only — no trading.
            Safe to call in SPOT ONLY mode (read-only).

        Returns list of dicts:
        {
            symbol: str,
            funding_rate: float,
            annualized_pct: float,
            basis_pct: float,
            mark_price: float,
            index_price: float,
            score: float,
        }
        """
        import requests

        opportunities = []

        # Get all premium index data
        # FIX A6: Add environment variable gate for futures API calls
        import os

        if os.environ.get("ENABLE_FUTURES", "").lower() not in ("true", "1", "yes"):
            logger.debug(
                "Futures API disabled (ENABLE_FUTURES not set). Skipping funding arb scan."
            )
            return []

        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error("Futures API error: HTTP %d", resp.status_code)
                return []
            premiums = resp.json()
        except Exception as e:
            logger.error("Failed to fetch premium data: %s", e)
            return []

        for item in premiums:
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            if symbols and sym not in symbols:
                continue

            fr = float(item.get("lastFundingRate", 0))
            mark = float(item.get("markPrice", 0))
            index = float(item.get("indexPrice", 0))

            # Only interested in positive funding (we short perp, receive payments)
            if fr < self.MIN_FUNDING_RATE:
                continue

            # Basis = spot premium over perp
            # We approximate spot as index price, perp as mark price
            basis_pct = ((index - mark) / mark) * 100 if mark > 0 else 0

            # Skip if basis too high (expensive to enter)
            if basis_pct > self.MAX_BASIS_PCT:
                continue

            # Annualized funding rate
            annualized = fr * 3 * 365 * 100  # 3 settlements/day * 365 days

            # Score: higher funding = better, lower basis = better
            score = min(100, annualized / 0.5)  # 50% annual = 100 score
            score = max(0, score - abs(basis_pct) * 20)  # penalize basis

            opportunities.append(
                {
                    "symbol": sym,
                    "funding_rate": fr,
                    "funding_rate_pct": fr * 100,
                    "annualized_pct": round(annualized, 1),
                    "basis_pct": round(basis_pct, 3),
                    "mark_price": mark,
                    "index_price": index,
                    "score": round(score, 1),
                    "next_funding_time": item.get("nextFundingTime"),
                }
            )

        # Sort by score
        opportunities.sort(key=lambda x: -x["score"])
        return opportunities

    def open_position(self, symbol: str, capital_usdt: float) -> Optional[Dict]:
        """Open a funding rate arbitrage position.

        ⚠️  REQUIRES FUTURES API — currently disabled (SPOT ONLY system).
            Returns None unconditionally.

        Original strategy: Buy spot + Short perp (equal notional).
        """
        logger.error(
            "FundingArb: BLOCKED — SPOT ONLY system, futures API not available"
        )
        return None

    def check_positions(self) -> List[Dict]:
        """Check all open funding arb positions.

        ⚠️  REQUIRES FUTURES API — currently disabled (SPOT ONLY system).
            Returns empty list unconditionally.
        """
        logger.error(
            "FundingArb: BLOCKED — SPOT ONLY system, futures API not available"
        )
        return []

    def _close_position(self, symbol: str, pos: Dict, reason: str):
        """Close both spot and futures legs of an arbitrage position.

        ⚠️  REQUIRES FUTURES API — currently disabled.
        """
        logger.error("FundingArb: BLOCKED — cannot close position in SPOT ONLY mode")

    def get_status(self) -> Dict:
        """Get current funding arbitrage status and P&L."""
        return {
            "positions": self._state.get("positions", {}),
            "total_positions": len(self._state.get("positions", {})),
            "history_count": len(self._state.get("history", [])),
        }

    def format_report(self) -> str:
        """Format funding arb report.

        ⚠️  SPOT ONLY mode — scan data only, no positions can be opened.
        """
        opps = self.scan_opportunities()
        status = self.get_status()

        lines = ["💰 Funding Rate 套利掃描"]
        lines.append("")

        if not opps:
            lines.append("無符合條件的機會（需 funding > 0.03%/8h）")
        else:
            lines.append(f"發現 {len(opps)} 個機會：")
            for o in opps[:10]:
                lines.append(
                    f"  {o['symbol']}: FR={o['funding_rate_pct']:.4f}% "
                    f"(年化 {o['annualized_pct']:.0f}%) "
                    f"基差={o['basis_pct']:+.3f}% "
                    f"評分={o['score']:.0f}"
                )

        pos = status["positions"]
        if pos:
            lines.append("")
            lines.append(f"活躍套利持倉: {status['total_positions']}")
            for sym, p in pos.items():
                lines.append(
                    f"  {sym}: 資金=${p.get('capital',0):.2f} 累計收益=${p.get('earned',0):.4f}"
                )

        return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src.binance_client import BinanceClient

    parser = argparse.ArgumentParser(description="Funding Arb CLI")
    parser.add_argument(
        "action",
        choices=["scan", "report", "status", "check"],
        help="scan: find opportunities, report: full report, status: brief status, check: check positions",
    )
    parser.add_argument(
        "--symbols",
        default="BTC,ETH,BNB,SOL,AVAX,NEAR,SUI,SEI,BARD,XRP,ADA",
        help="Comma-separated symbols to scan",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=70,
        help="Minimum opportunity score to auto-open (default: 70)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=1000,
        help="Capital per position in USDT (default: 1000)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Scan only, do not open positions"
    )
    args = parser.parse_args()

    client = BinanceClient()
    arb = FundingArbitrage(client, futures_client=None)

    if args.action == "scan":
        symbols = [s.strip() for s in args.symbols.split(",")]
        print(f"Scanning {len(symbols)} symbols for funding arb opportunities...")
        opps = arb.scan_opportunities(symbols=symbols)
        if not opps:
            print("No opportunities found.")
        else:
            print(f"\nFound {len(opps)} opportunities:")
            for o in sorted(opps, key=lambda x: x["score"], reverse=True):
                print(
                    f"  {o['symbol']}: FR={o['funding_rate']*100:.4f}%/8h "
                    f"({o['annualized_pct']:.0f}% annualized) score={o['score']:.0f} "
                    f"basis={o['basis_pct']:+.3f}%"
                )
                if not args.dry_run and o["score"] >= args.min_score:
                    print(f"    -> Opening position with ${args.capital}...")
                    result = arb.open_position(o["symbol"], capital_usdt=args.capital)
                    if result:
                        print(f"    -> SUCCESS: {result}")
                    else:
                        print("    -> FAILED")
                    print()
    elif args.action == "report":
        print(arb.format_report())
    elif args.action == "status":
        status = arb.get_status()
        print(f"Active positions: {status['total_positions']}")
        for sym, p in status["positions"].items():
            print(
                f"  {sym}: capital=${p.get('capital',0):.2f}, earned=${p.get('earned',0):.4f}"
            )
    elif args.action == "check":
        arb.check_positions()
        print("Position check complete.")
