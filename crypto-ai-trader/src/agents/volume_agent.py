"""
Volume Agent — Factor 3 (Volume Rank + Surge)

Wraps ``MarketScanner._factor_volume_momentum`` into an independently
testable component.  Scores volume rank, 24h price change, and
intra-hour volume surge.

Weight in overall scoring: 10%.
"""

import logging
from typing import Dict, List, Optional

from .base import SpecialistResult

logger = logging.getLogger(__name__)


class VolumeAgent:
    """Scores volume rank, 24h price change, and volume surge."""

    def analyze(
        self,
        coin_data: Optional[Dict] = None,
        volume_surge: bool = False,
    ) -> SpecialistResult:
        """Analyze volume metrics.

        Args:
            coin_data: Dict with at least ``rank`` (int),
                ``quote_volume`` (float), and optional ``volume_surge`` key.
            volume_surge: Explicit override for surge detection.

        Returns:
            SpecialistResult with score 0-100.
        """
        # Early return for empty/missing data
        if not coin_data:
            return SpecialistResult(
                score=50.0, signals=["⚠️ No data"], data={}, confidence="none"
            )

        signals: List[str] = []
        data: Dict = {}

        # Allow volume_surge to be set via coin_data or argument
        if "volume_surge" not in coin_data:
            coin_data["volume_surge"] = volume_surge

        score = self._factor_volume_momentum(coin_data)

        # --- signals --------------------------------------------------------
        rank = coin_data.get("rank", 999)
        if rank <= 10:
            signals.append(f"📊 Top {rank} Volume (24h)")
        elif rank <= 20:
            signals.append(f"📊 Top {rank} Volume (24h)")

        price_change = coin_data.get("price_change_24h", 0)
        if price_change > 10:
            signals.append(f"🚀 Strong 24h Rally (+{price_change:.1f}%)")
        elif price_change < -10:
            signals.append(f"📉 Sharp 24h Drop ({price_change:.1f}%)")

        if volume_surge or coin_data.get("volume_surge"):
            signals.append("🌊 1h Volume Surge (1.5x avg)")

        # --- data ------------------------------------------------------------
        data["rank"] = rank
        data["price_change_24h"] = price_change
        data["volume_surge"] = bool(volume_surge or coin_data.get("volume_surge"))

        confidence = "high" if score >= 60 or score <= 25 else "medium"

        return SpecialistResult(
            score=round(score, 2), signals=signals, data=data, confidence=confidence
        )

    # ------------------------------------------------------------------
    # Factor 3: Volume / Momentum
    # Mirrors MarketScanner._factor_volume_momentum exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_volume_momentum(coin_data: Dict) -> float:
        score = 0.0

        rank = coin_data.get("rank", 999)
        if rank <= 10:
            score += 30
        elif rank <= 20:
            score += 20
        elif rank <= 30:
            score += 10
        else:
            score += 5

        price_change = coin_data.get("price_change_24h", 0)
        if 0 < price_change <= 5:
            score += 20
        elif 5 < price_change <= 15:
            score += 30
        elif price_change > 15:
            score += 15
        elif -5 <= price_change <= 0:
            score += 10
        elif price_change < -5:
            score += 0

        if coin_data.get("volume_surge", False):
            score += 40

        return max(0.0, min(100.0, score))
