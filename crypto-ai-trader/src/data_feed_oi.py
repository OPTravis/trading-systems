"""
Open Interest data feed.

Fetches Open Interest data from Binance Futures (public, no auth).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class OpenInterest:
    """Fetch Open Interest data from Binance Futures (public, no auth).

    Provides current OI, historical OI, OI change percentage, and
    top symbols by open interest.
    """

    FUTURES_BASE = "https://fapi.binance.com"

    def __init__(self) -> None:
        self._session = requests.Session()

    # ------------------------------------------------------------------
    def get_open_interest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current open interest for a single symbol.

        ⚠️  Requires ENABLE_FUTURES=true environment variable.
            Returns None if futures API is disabled.

        Args:
            symbol: Futures trading pair (e.g. 'BTCUSDT').

        Returns:
            {symbol, open_interest: float, time: str} or None on failure.
        """
        if os.environ.get("ENABLE_FUTURES", "").lower() not in ("true", "1", "yes"):
            logger.debug(
                "Futures API disabled (ENABLE_FUTURES not set). Skipping OI fetch."
            )
            return None
        try:
            resp = self._session.get(
                f"{self.FUTURES_BASE}/fapi/v1/openInterest",
                params={"symbol": symbol.upper()},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "symbol": data["symbol"],
                "open_interest": float(data["openInterest"]),
                "time": data["time"],
            }
        except Exception as e:
            logger.error("Failed to fetch open interest for %s: %s", symbol, e)
            return None

    # ------------------------------------------------------------------
    def get_oi_history(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> List[Dict[str, Any]]:
        """Get historical open interest for a symbol.

        ⚠️  Requires ENABLE_FUTURES=true environment variable.
            Returns empty list if futures API is disabled.

        Args:
            symbol: Futures trading pair (e.g. 'BTCUSDT').
            period: Kline interval ('5m', '15m', '30m', '1h', '2h', '4h',
                     '6h', '12h', '1d').
            limit:  Number of data points (max 500).

        Returns:
            List of {symbol, sum_open_interest, sum_open_value, timestamp}.
        """
        if os.environ.get("ENABLE_FUTURES", "").lower() not in ("true", "1", "yes"):
            logger.debug(
                "Futures API disabled (ENABLE_FUTURES not set). Skipping OI history fetch."
            )
            return []
        try:
            resp = self._session.get(
                f"{self.FUTURES_BASE}/futures/data/openInterestHist",
                params={  # type: ignore[arg-type]
                    "symbol": symbol.upper(),
                    "period": period,
                    "limit": limit,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return [
                {
                    "symbol": symbol.upper(),
                    "sum_open_interest": float(d.get("sumOpenInterest", 0)),
                    "sum_open_value": float(
                        d.get("sumOpenInterestValue", d.get("sumOpenValue", 0))
                    ),
                    "timestamp": d["timestamp"],
                }
                for d in resp.json()
            ]
        except Exception as e:
            logger.error("Failed to fetch OI history for %s: %s", symbol, e)
            return []

    # ------------------------------------------------------------------
    def get_oi_change_pct(self, symbol: str, hours: int = 24) -> Optional[float]:
        """Calculate the percentage change in open interest over the last N hours.

        Args:
            symbol: Futures trading pair (e.g. 'BTCUSDT').
            hours:  Lookback window in hours.

        Returns:
            Float percentage (positive = OI increasing) or None on failure.
        """
        try:
            history = self.get_oi_history(symbol, period="1h", limit=hours)
            if len(history) < 2:
                # Futures disabled or no data — not an error, skip silently
                return None

            oldest = history[0]["sum_open_interest"]
            newest = history[-1]["sum_open_interest"]

            if oldest == 0:
                return None

            change_pct = ((newest - oldest) / oldest) * 100
            return change_pct
        except Exception as e:
            logger.error("Failed to calculate OI change for %s: %s", symbol, e)
            return None

    # ------------------------------------------------------------------
    def get_top_oi_symbols(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get top futures symbols by open interest.

        ⚠️  Requires ENABLE_FUTURES=true environment variable.
            Returns empty list if futures API is disabled.

        Uses the 24hr ticker endpoint which includes openInterest for
        each futures pair. Filters for USDT-margined pairs and sorts
        by open interest descending.

        Args:
            limit: Max number of symbols to return.

        Returns:
            List of {symbol, open_interest, notional_value, price_change_pct}.
        """
        if os.environ.get("ENABLE_FUTURES", "").lower() not in ("true", "1", "yes"):
            logger.debug(
                "Futures API disabled (ENABLE_FUTURES not set). Skipping top OI fetch."
            )
            return []
        try:
            resp = self._session.get(
                f"{self.FUTURES_BASE}/fapi/v1/ticker/24hr",
                timeout=15,
            )
            resp.raise_for_status()
            tickers = resp.json()

            # Filter USDT-margined perpetual pairs and sort by OI descending
            usdt_tickers = [
                t
                for t in tickers
                if t.get("symbol", "").endswith("USDT")
                and float(t.get("openInterest", 0)) > 0
            ]
            usdt_tickers.sort(key=lambda t: float(t["openInterest"]), reverse=True)

            results: List[Dict[str, Any]] = []
            for t in usdt_tickers[:limit]:
                oi_amount = float(t["openInterest"])
                last_price = float(t.get("lastPrice", 0))
                results.append(
                    {
                        "symbol": t["symbol"],
                        "open_interest": oi_amount,
                        "notional_value": oi_amount * last_price,
                        "price_change_pct": float(t.get("priceChangePercent", 0)),
                    }
                )

            return results
        except Exception as e:
            logger.error("Failed to fetch top OI symbols: %s", e)
            return []

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying HTTP session."""
        try:
            self._session.close()
        except Exception:
            logger.error("Failed to close OI session", exc_info=True)
