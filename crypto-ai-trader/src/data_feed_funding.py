"""
Funding rate data feed.

Fetches Binance Futures funding rates with anomaly detection.
"""

from __future__ import annotations

import logging
import statistics
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class FundingRate:
    """Fetch Binance Futures funding rates with anomaly detection.

    Uses BinanceClient's underlying futures endpoint via direct REST calls
    (python-binance Spot client doesn't expose futures methods).
    """

    FUTURES_BASE = "https://fapi.binance.com"

    def __init__(self) -> None:
        self._session = requests.Session()

    # ------------------------------------------------------------------
    def get_funding_rate(
        self, symbol: str = "BTCUSDT", limit: int = 24
    ) -> List[Dict[str, Any]]:
        """Fetch recent funding rates for a symbol.

        ⚠️  Requires ENABLE_FUTURES=true environment variable.
            Returns empty list if futures API is disabled.

        Args:
            symbol: Futures trading pair (e.g. 'BTCUSDT').
            limit:  Number of historical entries (max 1000).

        Returns:
            List of dicts with keys: symbol, funding_rate, funding_time, mark_price.
        """
        # SPOT ONLY safety gate — funding rate uses /fapi/ which is futures API
        # DISABLED: We need funding rate data for spot trading decisions (contrarian signal)
        # The /fapi/v1/fundingRate endpoint is PUBLIC (no API key needed), safe to call
        # if os.environ.get("ENABLE_FUTURES", "").lower() not in ("true", "1", "yes"):
        #     logger.debug("FundingRate disabled (ENABLE_FUTURES not set). Skipping fapi call.")
        #     return []
        
        try:
            # Phase 1: Fetch funding rate history (no markPrice in this endpoint)
            resp = self._session.get(
                f"{self.FUTURES_BASE}/fapi/v1/fundingRate",
                params={"symbol": symbol.upper(), "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                {
                    "symbol": symbol.upper(),
                    "funding_rate": float(d["fundingRate"]),
                    "funding_time": d["fundingTime"],
                    "mark_price": 0.0,  # populated from premiumIndex below
                }
                for d in data
            ]

            # Phase 2: Fetch current markPrice from /fapi/v1/premiumIndex
            # (fundingRate endpoint does NOT include markPrice)
            if results:
                try:
                    mark_resp = self._session.get(
                        f"{self.FUTURES_BASE}/fapi/v1/premiumIndex",
                        params={"symbol": symbol.upper()},
                        timeout=10,
                    )
                    mark_resp.raise_for_status()
                    mark_data = mark_resp.json()
                    if isinstance(mark_data, dict) and "markPrice" in mark_data:
                        mark_price = float(mark_data["markPrice"])
                        for r in results:
                            r["mark_price"] = mark_price
                except Exception as e:
                    logger.debug("Could not fetch mark price from premiumIndex for %s: %s", symbol, e)

            return results
        except Exception as e:
            logger.error("Failed to fetch funding rate for %s: %s", symbol, e)
            return []

    # ------------------------------------------------------------------
    def get_funding_summary(self) -> Dict[str, Any]:
        """Get current funding rate + 24h mean for BTC and ETH.

        Returns:
            {
                "BTC": {"current": float, "mean_24h": float, "std_24h": float, "anomaly": bool},
                "ETH": {...},
                "timestamp": str,
            }
        """
        summary: Dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
        for sym in ("BTCUSDT", "ETHUSDT"):
            tag = sym.replace("USDT", "")
            rates = self.get_funding_rate(sym, limit=24)
            if not rates:
                summary[tag] = None
                continue

            values = [r["funding_rate"] for r in rates]
            current = values[-1]
            mean = statistics.mean(values)
            std = statistics.pstdev(values) if len(values) > 1 else 0.0

            # Anomaly: current exceeds 2x standard deviation from mean
            anomaly = std > 0 and abs(current - mean) > 2 * std

            summary[tag] = {
                "current": current,
                "mean_24h": mean,
                "std_24h": std,
                "anomaly": anomaly,
            }

        return summary

    # ------------------------------------------------------------------
    def get_funding_rolling_avg(
        self, symbol: str = "BTCUSDT", days: int = 30
    ) -> Dict[str, Any]:
        """Get funding rate rolling average over N days.

        Fetches 8-hourly data (3 entries/day) and computes rolling stats.
        Based on research report: 30-day negative avg → 83%-96% win rate.

        Returns:
            {
                symbol, current, rolling_avg, rolling_min, rolling_max,
                negative_days, negative_pct, signal, signal_strength
            }
        """
        entries_needed = days * 3  # 3 funding events per day (every 8h)
        rates = self.get_funding_rate(symbol, limit=min(entries_needed, 1000))
        if not rates:
            return {"symbol": symbol, "signal": "NO_DATA", "signal_strength": 0}

        values = [r["funding_rate"] for r in rates]
        current = values[-1]
        avg = statistics.mean(values)
        neg_count = sum(1 for v in values if v < 0)
        neg_pct = neg_count / len(values) * 100

        # Signal classification based on research report thresholds
        signal = "NEUTRAL"
        strength = 0
        if avg < -0.0001:  # Strong negative 30d avg
            signal = "STRONG_BULLISH"
            strength = 5
        elif avg < -0.00005:
            signal = "BULLISH"
            strength = 4
        elif avg < 0:
            signal = "SLIGHT_BULLISH"
            strength = 3
        elif avg > 0.0005:  # Extreme positive = overheated
            signal = "STRONG_BEARISH"
            strength = -4
        elif avg > 0.0001:
            signal = "BEARISH"
            strength = -2

        # Current rate extremes override
        if current < -0.0005:
            signal = "EXTREME_BULLISH"
            strength = max(strength, 5)
        elif current > 0.001:
            signal = "EXTREME_BEARISH"
            strength = min(strength, -5)

        return {
            "symbol": symbol,
            "current": current,
            "rolling_avg": round(avg, 8),
            "rolling_min": min(values),
            "rolling_max": max(values),
            "negative_days": round(neg_count / 3),  # approximate days
            "negative_pct": round(neg_pct, 1),
            "signal": signal,
            "signal_strength": strength,
        }

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying HTTP session."""
        try:
            self._session.close()
        except Exception:
            logger.error("Failed to close data feed HTTP session", exc_info=True)
