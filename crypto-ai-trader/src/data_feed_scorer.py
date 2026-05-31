"""
Scoring data aggregator.

Combines FundingRate + OpenInterest data into sentiment scores.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.data_feed_funding import FundingRate
from src.data_feed_oi import OpenInterest

logger = logging.getLogger(__name__)


class ScoringDataAggregator:
    """Combine FundingRate + OpenInterest data into sentiment scores.

    Produces per-symbol sentiment scores that factor in funding rate
    direction (contrarian signal) and open interest changes with
    price direction (momentum / liquidation signals).
    """

    def __init__(self, funding: FundingRate, oi: OpenInterest) -> None:
        self.funding = funding
        self.oi = oi

    # ------------------------------------------------------------------
    def get_symbol_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Compute a sentiment score for a single symbol.

        Scoring logic (funding rate component):
            very negative  (< -0.01%):  +15  (overshorted, potential squeeze)
            slightly neg   (-0.01% to 0%): +8
            neutral        (0% to 0.01%):   +3
            positive norm  (0.01% to 0.05%): 0
            high           (> 0.05%):        -5  (crowded long)
            very high      (> 0.1%):         -15 (extremely crowded)

        Scoring logic (OI + price direction component):
            OI up  + price up:   +5  (new money entering long)
            OI up  + price down: -5  (new money entering short)
            OI down + price up:  +3  (shorts covering)
            OI down + price down: -3  (longs liquidating)

        Args:
            symbol: Futures trading pair (e.g. 'BTCUSDT').

        Returns:
            {sentiment_score, funding_rate, oi_change_pct, signals}
        """
        signals: List[str] = []
        score = 0.0
        funding_rate_val = 0.0
        oi_change_pct: Optional[float] = None

        # --- Funding rate component ---
        funding_data = self.funding.get_funding_rate(symbol, limit=8)
        if funding_data:
            funding_rate_val = funding_data[-1]["funding_rate"]
            rate_pct = funding_rate_val * 100  # convert to percentage

            if rate_pct < -0.01:
                score += 15
                signals.append("funding_very_negative_overshorted")
            elif rate_pct < 0:
                score += 8
                signals.append("funding_slightly_negative")
            elif rate_pct <= 0.01:
                score += 3
                signals.append("funding_neutral")
            elif rate_pct <= 0.05:
                score += 0
                signals.append("funding_positive_normal")
            elif rate_pct <= 0.1:
                score -= 5
                signals.append("funding_high_crowded_long")
            else:
                score -= 15
                signals.append("funding_very_high_extremely_crowded")

        # --- Open interest + price direction component ---
        oi_change_pct = self.oi.get_oi_change_pct(symbol, hours=24)
        if oi_change_pct is not None:
            oi_increasing = oi_change_pct > 0

            # Determine price direction from mark price in funding data
            price_up: Optional[bool] = None
            if funding_data and len(funding_data) >= 2:
                price_up = (
                    funding_data[-1]["mark_price"] > funding_data[0]["mark_price"]
                )

            if price_up is not None:
                if oi_increasing and price_up:
                    score += 5
                    signals.append("oi_increasing_price_up_new_long_money")
                elif oi_increasing and not price_up:
                    score -= 5
                    signals.append("oi_increasing_price_down_new_short_money")
                elif not oi_increasing and price_up:
                    score += 3
                    signals.append("oi_decreasing_price_up_shorts_covering")
                else:
                    score -= 3
                    signals.append("oi_decreasing_price_down_longs_liquidating")
            else:
                signals.append("oi_change_no_price_data")

        return {
            "sentiment_score": score,
            "funding_rate": funding_rate_val,
            "oi_change_pct": oi_change_pct,
            "signals": signals,
        }

    # ------------------------------------------------------------------
    def get_market_funding_snapshot(
        self, symbols: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Batch compute sentiment for multiple symbols.

        Fetches sequentially with a small delay between calls to
        avoid Binance rate limits. Caps at 20 symbols.

        Args:
            symbols: List of futures trading pairs (e.g. ['BTCUSDT', ...]).

        Returns:
            {symbol: {sentiment_score, funding_rate, oi_change_pct, signals}}
        """
        capped = symbols[:20]
        results: Dict[str, Dict[str, Any]] = {}

        for sym in capped:
            try:
                results[sym.upper()] = self.get_symbol_sentiment(sym)
            except Exception as e:
                logger.error("Sentiment scoring failed for %s: %s", sym, e)
                results[sym.upper()] = {
                    "sentiment_score": 0.0,
                    "funding_rate": 0.0,
                    "oi_change_pct": None,
                    "signals": [],
                    "error": str(e),
                }
            # Small delay to respect rate limits
            if sym != capped[-1]:
                time.sleep(0.15)

        return results
