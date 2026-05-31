"""
Sentiment Agent — Factor 4 (Funding Rate + OI)

Wraps ``MarketScanner._factor_sentiment`` into an independently
testable component.  Scores funding rate and open-interest changes
with a contrarian bias.

Weight in overall scoring: 8%.
"""

import logging
from typing import Dict, List, Optional

from .base import SpecialistResult

logger = logging.getLogger(__name__)


class SentimentAgent:
    """Scores funding rate and OI changes with contrarian bias."""

    def analyze(
        self,
        funding_data: Optional[Dict] = None,
    ) -> SpecialistResult:
        """Analyze funding / OI sentiment data.

        Args:
            funding_data: Dict with at least ``sentiment_score`` (float,
                -15 to +15), ``funding_rate`` (float), and
                ``oi_change_pct`` (float).

        Returns:
            SpecialistResult with score 0-100.
        """
        # Early return for empty/missing data
        if not funding_data:
            return SpecialistResult(
                score=50.0, signals=["⚠️ No data available"], data={}, confidence="none"
            )

        signals: List[str] = []
        data: Dict = {}

        score = self._factor_sentiment(funding_data)

        # --- signals --------------------------------------------------------
        if funding_data:
            sent_score = funding_data.get("sentiment_score", 0)
            funding = funding_data.get("funding_rate")
            oi_change = funding_data.get("oi_change_pct")

            if sent_score >= 8:
                signals.append(f"😊 Strong Positive Sentiment ({sent_score:.1f})")
            elif sent_score <= -8:
                signals.append(f"😰 Strong Negative Sentiment ({sent_score:.1f})")

            if funding is not None:
                if funding < -0.01:
                    signals.append(
                        f"💚 Negative Funding ({funding:.4f}) — contrarian bullish"
                    )
                elif funding > 0.03:
                    signals.append(
                        f"💸 High Funding ({funding:.4f}) — overleveraged longs"
                    )

            if oi_change is not None:
                if oi_change > 10:
                    signals.append(f"📈 OI Surge +{oi_change:.1f}%")
                elif oi_change < -10:
                    signals.append(f"📉 OI Drop {oi_change:.1f}%")

        # --- data ------------------------------------------------------------
        if funding_data:
            data["sentiment_score"] = funding_data.get("sentiment_score", 0)
            data["funding_rate"] = funding_data.get("funding_rate")
            data["oi_change_pct"] = funding_data.get("oi_change_pct")
        else:
            data["sentiment_score"] = 0
            data["funding_rate"] = None
            data["oi_change_pct"] = None

        confidence = "medium" if funding_data else "low"

        return SpecialistResult(
            score=round(score, 2), signals=signals, data=data, confidence=confidence
        )

    # ------------------------------------------------------------------
    # Factor 4: Sentiment (funding + OI)
    # Mirrors MarketScanner._factor_sentiment exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_sentiment(sentiment_data: Optional[Dict]) -> float:
        if sentiment_data is None:
            return 50.0

        sentiment_score = sentiment_data.get("sentiment_score", 0)
        mapped = 50.0 + sentiment_score * 3.33
        return max(0.0, min(100.0, mapped))
