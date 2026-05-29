"""
PrePump Agent — Factors 6 + 7 (OBV Divergence + Consolidation Breakout)

Wraps ``MarketScanner._factor_obv_divergence`` and
``MarketScanner._factor_consolidation`` into an independently testable
component.

Factors covered:
  6. OBV Divergence — smart money accumulation detection
  7. Consolidation Breakout — long-range breakout with volume

Each factor is scored 0-100; the blended score normalises the two
factors (each 8% of overall) into a single 0-100 result.

Weight in overall scoring: 8% + 8% = 16%.
"""

import logging
from typing import Dict, List, Optional

from .base import SpecialistResult

logger = logging.getLogger(__name__)


class PrePumpAgent:
    """Scores OBV divergence and consolidation breakout for early detection."""

    def analyze(
        self,
        obv_div_data: Optional[Dict] = None,
        consolidation_data: Optional[Dict] = None,
        klines_1h: Optional[List[Dict]] = None,
        klines_4h: Optional[List[Dict]] = None,
    ) -> SpecialistResult:
        """Analyze pre-pump indicators.

        Either pass pre-computed dicts *or* raw klines (the indicators
        will be computed on the fly when klines are provided).

        Args:
            obv_div_data: Pre-computed ``Indicators.obv_divergence`` output.
            consolidation_data: Pre-computed ``Indicators.consolidation_breakout`` output.
            klines_1h: Raw 1h klines (≥35 candles) — used to compute OBV divergence
                if ``obv_div_data`` is ``None``.
            klines_4h: Raw 4h klines (≥35 candles) — used to compute consolidation
                breakout if ``consolidation_data`` is ``None``.

        Returns:
            SpecialistResult with score 0-100.
        """
        # Early return for empty/missing data
        if not obv_div_data and not consolidation_data and not klines_1h and not klines_4h:
            return SpecialistResult(score=50.0, signals=['⚠️ No data available'], data={}, confidence='none')

        signals: List[str] = []
        data: Dict = {}

        # --- compute from klines if not provided ----------------------------
        if obv_div_data is None and klines_1h is not None and len(klines_1h) >= 35:
            from ..indicators import Indicators
            obv_div_data = Indicators.obv_divergence(klines_1h, lookback=20)

        if consolidation_data is None and klines_4h is not None and len(klines_4h) >= 35:
            from ..indicators import Indicators
            consolidation_data = Indicators.consolidation_breakout(klines_4h)

        # --- sub-factor scores (each 0-100) --------------------------------
        f_obv = self._factor_obv_divergence(obv_div_data)
        f_consolidation = self._factor_consolidation(consolidation_data)

        # --- weighted blend (each 8% of total → 50/50 within this agent) ---
        score = 0.5 * f_obv + 0.5 * f_consolidation

        # --- signals ---------------------------------------------------------
        if obv_div_data:
            if obv_div_data.get("detected"):
                signals.append(
                    f"🐋 OBV Bullish Divergence (strength: {obv_div_data['strength']:.0f})"
                )
            elif obv_div_data.get("obv_trend") == "rising":
                signals.append("📊 OBV Rising Trend")

        if consolidation_data:
            if consolidation_data.get("breaking_out"):
                vol_tag = " + Volume" if consolidation_data.get("volume_confirmed") else ""
                signals.append(
                    f"🚀 Consolidation Breakout ({consolidation_data['days_in_range']}d range{vol_tag})"
                )
            elif consolidation_data.get("in_consolidation"):
                signals.append(
                    f"📦 In Consolidation ({consolidation_data['days_in_range']}d, "
                    f"{consolidation_data['range_pct']:.1f}% range)"
                )

        # --- data ------------------------------------------------------------
        data['f_obv_divergence'] = round(f_obv, 2)
        data['f_consolidation'] = round(f_consolidation, 2)
        if obv_div_data:
            data['obv_detected'] = obv_div_data.get("detected", False)
            data['obv_trend'] = obv_div_data.get("obv_trend", "unknown")
        if consolidation_data:
            data['breaking_out'] = consolidation_data.get("breaking_out", False)
            data['in_consolidation'] = consolidation_data.get("in_consolidation", False)

        confidence = 'high' if score >= 65 else 'medium' if score >= 45 else 'low'

        return SpecialistResult(score=round(score, 2), signals=signals, data=data, confidence=confidence)

    # ------------------------------------------------------------------
    # Factor 6: OBV Divergence
    # Mirrors MarketScanner._factor_obv_divergence exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_obv_divergence(obv_data: Optional[Dict]) -> float:
        if not obv_data:
            return 30.0

        if obv_data.get("detected"):
            strength = obv_data.get("strength", 0)
            base = 60 + min(40, strength * 0.4)
        else:
            base = 30.0

        if obv_data.get("obv_trend") == "rising":
            base = min(100, base + 20)

        return max(0.0, min(100.0, base))

    # ------------------------------------------------------------------
    # Factor 7: Consolidation Breakout
    # Mirrors MarketScanner._factor_consolidation exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_consolidation(consol_data: Optional[Dict]) -> float:
        if not consol_data:
            return 30.0

        if consol_data.get("breaking_out"):
            base = 80.0
            if consol_data.get("volume_confirmed"):
                base = 95.0
        elif consol_data.get("in_consolidation"):
            days = consol_data.get("days_in_range", 0)
            range_pct = consol_data.get("range_pct", 25)
            base = 50.0
            if days >= 40:
                base += 15
            elif days >= 30:
                base += 10
            if range_pct <= 15:
                base += 10
        else:
            base = 30.0

        return max(0.0, min(100.0, base))
