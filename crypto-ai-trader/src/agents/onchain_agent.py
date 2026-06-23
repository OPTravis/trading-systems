"""
On-Chain Agent — Factor 10 (DeFiLlama TVL)

Wraps the on-chain scoring into an independently testable component.
The DeFiLlama score is pre-fetched and passed in; this agent simply
validates, clips to 0-100, and emits signals.

Weight in overall scoring: 10%.
"""

import logging
from typing import Dict, List

from .base import SpecialistResult
from .calibration import apply_calibration

logger = logging.getLogger(__name__)


class OnChainAgent:
    """Scores on-chain TVL changes from DeFiLlama."""

    def analyze(self, onchain_score: float = 50.0) -> SpecialistResult:
        """Wrap a pre-computed on-chain score.

        Args:
            onchain_score: Score 0-100 from ``DataFeed.onchain.get_onchain_score()``.

        Returns:
            SpecialistResult with score 0-100.
        """

        # Early return for empty/missing data
        if onchain_score is None:
            return SpecialistResult(
                score=50.0, signals=["⚠️ No data"], data={}, confidence="none"
            )

        signals: List[str] = []
        data: Dict = {}

        score = max(0.0, min(100.0, float(onchain_score)))

        # --- signals --------------------------------------------------------
        if score >= 75:
            signals.append("🐋 Strong On-Chain Activity (TVL surge)")
        elif score >= 60:
            signals.append("📊 Positive On-Chain Trend")
        elif score <= 25:
            signals.append("📉 Weak On-Chain Activity")
        elif score <= 40:
            signals.append("⚠️ Declining On-Chain Metrics")

        # --- data ------------------------------------------------------------
        data["onchain_score_raw"] = round(onchain_score, 2)

        confidence = "high" if abs(score - 50) > 25 else "medium"

        # Apply outcome-based calibration
        score = self._apply_calibration(score)

        return SpecialistResult(
            score=round(score, 2), signals=signals, data=data, confidence=confidence
        )

    @staticmethod
    def _apply_calibration(raw_score: float) -> float:
        """Apply outcome-based calibration to the raw score."""
        return apply_calibration("onchain_agent", raw_score)
