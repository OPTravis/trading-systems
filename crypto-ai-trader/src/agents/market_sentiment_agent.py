"""
Market Sentiment Agent — Factor 11 (Fear & Greed Index)

Wraps the Fear & Greed contrarian scoring into an independently
testable component.  The F&G value is pre-fetched and passed in;
this agent applies the contrarian mapping and emits signals.

Weight in overall scoring: 10%.
"""

import logging
from typing import Dict, List

from .base import SpecialistResult

logger = logging.getLogger(__name__)


class MarketSentimentAgent:
    """Contrarian Fear & Greed Index scoring for LONG bias."""

    def analyze(self, fng_value: int = 50) -> SpecialistResult:
        """Analyze Fear & Greed index value with contrarian mapping.

        Args:
            fng_value: Raw F&G value 0-100 (0=Extreme Fear, 100=Extreme Greed).
                Contrarian mapping inverts for LONG bias.

        Returns:
            SpecialistResult with score 0-100.
        """
        # Early return for missing data
        if fng_value is None:
            return SpecialistResult(
                score=50.0, signals=["⚠️ No data"], data={}, confidence="none"
            )

        signals: List[str] = []
        data: Dict = {}

        score = self._contrarian_score(fng_value)

        # --- signals --------------------------------------------------------
        if fng_value <= 20:
            signals.append(f"😱 Extreme Fear ({fng_value}) — contrarian bullish")
        elif fng_value <= 35:
            signals.append(f"😰 Fear ({fng_value}) — contrarian opportunity")
        elif fng_value <= 45:
            signals.append(f"😟 Mild Fear ({fng_value})")
        elif fng_value <= 55:
            signals.append(f"😐 Neutral ({fng_value})")
        elif fng_value <= 70:
            signals.append(f"🙂 Mild Greed ({fng_value})")
        elif fng_value <= 85:
            signals.append(f"🤑 Greed ({fng_value}) — contrarian caution")
        else:
            signals.append(f"🔥 Extreme Greed ({fng_value}) — contrarian bearish")

        # --- data ------------------------------------------------------------
        data["fng_value"] = fng_value
        data["contrarian_score"] = round(score, 2)

        confidence = "high" if abs(fng_value - 50) > 30 else "medium"

        return SpecialistResult(
            score=round(score, 2), signals=signals, data=data, confidence=confidence
        )

    # ------------------------------------------------------------------
    # Contrarian F&G mapping — mirrors the inline logic in
    # MarketScanner._analyze_coin lines 222-232.
    # ------------------------------------------------------------------

    @staticmethod
    def _contrarian_score(fng_val: int) -> float:
        """Map raw Fear & Greed value (0-100) to contrarian LONG-bias score.

        Same piecewise mapping used in ``_analyze_coin``:
            ≤20  → 90 + (20-v)*0.5   (up to 100)
            ≤40  → 70 + (40-v)*1.0
            ≤60  → 50 + (60-v)*1.0
            ≤80  → 30 + (80-v)*1.0
            >80  → 10 + (100-v)*0.5
        """
        if fng_val <= 20:
            return 90.0 + (20 - fng_val) * 0.5
        elif fng_val <= 40:
            return 70.0 + (40 - fng_val) * 1.0
        elif fng_val <= 60:
            return 50.0 + (60 - fng_val) * 1.0
        elif fng_val <= 80:
            return 30.0 + (80 - fng_val) * 1.0
        else:
            return 10.0 + (100 - fng_val) * 0.5
