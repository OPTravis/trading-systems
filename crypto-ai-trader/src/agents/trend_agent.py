"""
Trend Agent — Factor 2 (Multi-Timeframe Trend Alignment)

Wraps ``MarketScanner._factor_trend_alignment`` into an independently
testable component.  Uses pre-computed multi-timeframe data (trend_score
from ``MultiTimeframeAnalyzer``).

Weight in overall scoring: 15%.
"""

import logging
from typing import Dict, List

from .base import SpecialistResult

logger = logging.getLogger(__name__)


class TrendAgent:
    """Scores multi-timeframe trend alignment."""

    def analyze(self, mtf_data: Dict) -> SpecialistResult:
        """Analyze pre-computed multi-timeframe data.

        Args:
            mtf_data: Dict from ``MultiTimeframeAnalyzer.analyze()`` containing
                ``trend_score`` (0-100), ``trend_alignment``, ``entry_signal``,
                ``tf_1h``, ``tf_4h``, ``tf_15m``.

        Returns:
            SpecialistResult with score 0-100.
        """
        # Early return for empty/missing data
        if not mtf_data:
            return SpecialistResult(score=50.0, signals=['⚠️ No data available'], data={}, confidence='none')

        signals: List[str] = []
        data: Dict = {}

        # --- trend_score is already 0-100 ---------------------------------
        trend_score = self._factor_trend_alignment(mtf_data)
        score = trend_score

        # --- signals --------------------------------------------------------
        trend_alignment = mtf_data.get("trend_alignment", "")
        entry_signal = mtf_data.get("entry_signal")

        if entry_signal:
            signals.append(f"✅ Multi-TF Entry Signal ({entry_signal})")
        if trend_alignment == "bullish":
            signals.append("🚀 Multi-TF Bullish Alignment")
        elif trend_alignment == "bearish":
            signals.append("📉 Multi-TF Bearish Alignment")

        if trend_score >= 75:
            signals.append(f"💪 Strong Trend Score: {trend_score:.0f}")
        elif trend_score >= 50:
            signals.append(f"📈 Moderate Trend Score: {trend_score:.0f}")

        # --- data ------------------------------------------------------------
        data['trend_score'] = round(trend_score, 2)
        data['trend_alignment'] = trend_alignment
        data['entry_signal'] = entry_signal

        confidence = 'high' if trend_score >= 70 or trend_score <= 30 else 'medium'

        return SpecialistResult(score=round(score, 2), signals=signals, data=data, confidence=confidence)

    # ------------------------------------------------------------------
    # Factor 2: Multi-TF Trend (direct passthrough of trend_score)
    # Mirrors MarketScanner._factor_trend_alignment exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_trend_alignment(mtf_result: Dict) -> float:
        return max(0.0, min(100.0, float(mtf_result.get("trend_score", 0))))
