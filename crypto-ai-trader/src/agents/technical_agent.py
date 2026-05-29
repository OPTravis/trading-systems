"""
Technical Agent — Factors 1 + 5 + 8 + 9

Wraps the inline technical, price-action, BB-squeeze, and RSI-divergence
calculations from ``MarketScanner._factor_*`` into an independently
testable component.

Factors covered:
  1. Technical (RSI, MACD, BB, VWAP, MA alignment) from 1h klines
  5. Price Action (volatility band + momentum)
  8. BB Squeeze (Bollinger compression)
  9. RSI Divergence (bullish RSI divergence)

Each factor is scored 0-100; the ``analyze`` method returns a blended
``SpecialistResult`` whose ``score`` is the weighted average matching
the weights used in ``_calculate_weighted_score``:
  technical  = 15% of total → 36% of this agent's blend
  price_act  =  8% of total → 20% of this agent's blend
  bb_squeeze =  4% of total → 10% of this agent's blend
  rsi_div    =  4% of total → 10% of this agent's blend
  (remaining 24% of the total weight is allocated to other agents)
"""

import logging
from typing import Dict, List, Optional

from .base import SpecialistResult

logger = logging.getLogger(__name__)


class TechnicalAgent:
    """Scores technical indicators, price action, BB squeeze, and RSI divergence."""

    # Relative weights for blending sub-factors (must sum to 1.0)
    _SUB_WEIGHTS = {
        'technical':  0.36,   # maps to overall 15%
        'price_act':  0.20,   # maps to overall 8%
        'bb_squeeze': 0.10,   # maps to overall 4%
        'rsi_div':    0.10,   # maps to overall 4%
        # remaining 0.24 is padding so we can normalise to the agent's
        # contribution if needed; but we'll just weight-sum directly.
    }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        klines_1h: Optional[List[Dict]] = None,
        tf_1h: Optional[Dict] = None,
        bb_squeeze_data: Optional[Dict] = None,
        rsi_div_data: Optional[Dict] = None,
    ) -> SpecialistResult:
        """Run technical analysis on pre-computed / pre-fetched data.

        Args:
            klines_1h: Raw 1h klines (≥50 candles). If supplied and
                ``tf_1h`` is ``None``, indicators are computed on the fly.
            tf_1h: Pre-computed 1h analysis dict (keys matching
                ``Indicators.analyze_symbol`` output). Takes precedence.
            bb_squeeze_data: Pre-computed ``Indicators.bb_squeeze`` output.
            rsi_div_data: Pre-computed ``Indicators.rsi_divergence`` output.

        Returns:
            SpecialistResult with score 0-100.
        """
        signals: List[str] = []
        data: Dict = {}

        # --- early return for empty/missing data ---
        # If no klines AND no pre-computed tf_1h, return neutral score
        if (klines_1h is None or len(klines_1h) == 0) and tf_1h is None:
            return SpecialistResult(
                score=50.0,
                signals=["⚠️ No data available"],
                data={'f_technical': 50.0, 'f_price_action': 50.0, 'f_bb_squeeze': 50.0, 'f_rsi_divergence': 50.0, 'rsi': 50, 'macd_histogram': 0},
                confidence='none'
            )

        # --- obtain tf_1h ---------------------------------------------------
        if tf_1h is None and klines_1h is not None and len(klines_1h) >= 50:
            from ..indicators import Indicators
            tf_1h = Indicators.analyze_symbol(klines_1h)
            # Also compute BB squeeze & RSI div from klines if not provided
            if bb_squeeze_data is None and len(klines_1h) >= 35:
                bb_squeeze_data = Indicators.bb_squeeze(klines_1h)
            if rsi_div_data is None and len(klines_1h) >= 35:
                rsi_div_data = Indicators.rsi_divergence(klines_1h)

        if tf_1h is None:
            tf_1h = {}

        # --- sub-factor scores (each 0-100) --------------------------------
        f_technical = self._factor_technical(tf_1h)
        f_price_act = self._factor_price_action(tf_1h)
        f_bb_sq = self._factor_bb_squeeze(bb_squeeze_data)
        f_rsi_div = self._factor_rsi_divergence(rsi_div_data)

        # --- weighted blend --------------------------------------------------
        # The percentages in the overall scanner are:
        #   technical 15%, price_action 8%, bb_squeeze 4%, rsi_div 4%
        # Sum = 31% of total. We normalise so this agent's score is 0-100.
        total_weight = 0.15 + 0.08 + 0.04 + 0.04
        score = (
            0.15 * f_technical
            + 0.08 * f_price_act
            + 0.04 * f_bb_sq
            + 0.04 * f_rsi_div
        ) / total_weight * 100
        score = max(0.0, min(100.0, score))

        # --- signals ---------------------------------------------------------
        signals.extend(self._technical_signals(tf_1h))
        signals.extend(self._price_action_signals(tf_1h))
        if bb_squeeze_data and bb_squeeze_data.get("squeezing"):
            signals.append(
                f"⚡ BB Squeeze (percentile: {bb_squeeze_data.get('percentile', 0):.0f}%)"
            )
        if rsi_div_data and rsi_div_data.get("detected"):
            signals.append(
                f"💎 RSI Bullish Divergence (strength: {rsi_div_data['strength']:.0f})"
            )

        # --- data ------------------------------------------------------------
        data['f_technical'] = round(f_technical, 2)
        data['f_price_action'] = round(f_price_act, 2)
        data['f_bb_squeeze'] = round(f_bb_sq, 2)
        data['f_rsi_divergence'] = round(f_rsi_div, 2)
        data['rsi'] = tf_1h.get('rsi', 50)
        data['macd_histogram'] = tf_1h.get('macd_histogram', 0)

        confidence = 'high' if f_technical >= 60 or f_technical <= 25 else 'medium'

        return SpecialistResult(score=round(score, 2), signals=signals, data=data, confidence=confidence)

    # ------------------------------------------------------------------
    # Factor 1: Technical (RSI, MACD, BB, VWAP, MA alignment)
    # Mirrors MarketScanner._factor_technical exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_technical(tf_1h: Dict) -> float:
        score = 0.0

        # RSI scoring (max 25 points)
        rsi = tf_1h.get("rsi", 50)
        if rsi < 20:
            score += 25
        elif rsi < 30:
            score += 22
        elif rsi < 40:
            score += 18
        elif rsi < 50:
            score += 12
        elif 50 <= rsi < 60:
            score += 10
        elif 60 <= rsi < 70:
            score += 12  # healthy bullish confirmation — was 0 (blind spot fix)
        elif rsi > 80:
            score += 3
        elif rsi > 70:
            score += 5

        # MACD histogram scoring (max 25 points — bidirectional fix)
        macd_hist = tf_1h.get("macd_histogram", 0)
        if macd_hist > 0:
            score += 25
        elif macd_hist < 0:
            score -= 10  # penalize bearish MACD — was 0 (one-way fix)

        # BB below lower (max 20 points)
        current_price = tf_1h.get("current_price", 0)
        bb_lower = tf_1h.get("bb_lower", 0)
        if current_price and bb_lower and current_price < bb_lower:
            score += 20

        # Price above VWAP (max 15 points)
        vwap = tf_1h.get("vwap", 0)
        if current_price and vwap and current_price > vwap:
            score += 15

        # MA alignment bullish (MA7 > MA25 > MA99) (max 15 points)
        ma7 = tf_1h.get("ma7", 0)
        ma25 = tf_1h.get("ma25", 0)
        ma99 = tf_1h.get("ma99", 0)
        if ma7 > ma25 > ma99:
            score += 15

        return max(0.0, min(100.0, score))

    # ------------------------------------------------------------------
    # Factor 5: Price Action (volatility + momentum)
    # Mirrors MarketScanner._factor_price_action exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_price_action(tf_1h: Dict) -> float:
        score = 0.0

        vol = tf_1h.get("volatility_pct", 0)
        if 2 <= vol <= 8:
            score += 80
        elif 8 < vol <= 15:
            score += 50
        elif vol > 15:
            score += 20
        else:
            score += 30

        momentum = tf_1h.get("momentum", 0)
        if momentum > 0:
            score += 20

        return max(0.0, min(100.0, score))

    # ------------------------------------------------------------------
    # Factor 8: BB Squeeze
    # Mirrors MarketScanner._factor_bb_squeeze exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_bb_squeeze(squeeze_data: Optional[Dict]) -> float:
        if not squeeze_data:
            return 30.0

        if squeeze_data.get("squeezing"):
            pctile = squeeze_data.get("percentile", 50)
            base = 90 - pctile
        else:
            percentile = squeeze_data.get("percentile", 50)
            if percentile <= 35:
                base = 50.0
            else:
                base = 30.0

        return max(0.0, min(100.0, base))

    # ------------------------------------------------------------------
    # Factor 9: RSI Divergence
    # Mirrors MarketScanner._factor_rsi_divergence exactly.
    # ------------------------------------------------------------------

    @staticmethod
    def _factor_rsi_divergence(rsi_div_data: Optional[Dict]) -> float:
        if not rsi_div_data:
            return 30.0

        if rsi_div_data.get("detected"):
            strength = rsi_div_data.get("strength", 0)
            base = 70 + min(30, strength * 0.3)
        else:
            rsi = rsi_div_data.get("rsi_current", 50)
            if rsi < 30:
                base = 50.0
            elif rsi < 40:
                base = 40.0
            else:
                base = 30.0

        return max(0.0, min(100.0, base))

    # ------------------------------------------------------------------
    # Signal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _technical_signals(tf_1h: Dict) -> List[str]:
        signals: List[str] = []

        trend = tf_1h.get("trend", "")
        if trend == "strong_up":
            signals.append("🚀 1h Strong Uptrend")
        elif trend == "strong_down":
            signals.append("📉 1h Strong Downtrend")

        rsi = tf_1h.get("rsi", 50)
        if rsi < 30:
            signals.append(f"💎 RSI Oversold ({rsi:.1f})")
        elif rsi > 70:
            signals.append(f"🔥 RSI Overbought ({rsi:.1f})")

        current_price = tf_1h.get("current_price", 0)
        vwap = tf_1h.get("vwap", 0)
        if current_price and vwap:
            if current_price > vwap:
                signals.append("📈 Above VWAP")
            else:
                signals.append("📉 Below VWAP")

        bb_lower = tf_1h.get("bb_lower", 0)
        bb_upper = tf_1h.get("bb_upper", 0)
        if current_price and bb_lower and current_price < bb_lower:
            signals.append("🎯 Below Lower Bollinger Band")
        elif current_price and bb_upper and current_price > bb_upper:
            signals.append("🎯 Above Upper Bollinger Band")

        return signals

    @staticmethod
    def _price_action_signals(tf_1h: Dict) -> List[str]:
        signals: List[str] = []
        vol = tf_1h.get("volatility_pct", 0)
        momentum = tf_1h.get("momentum", 0)
        if vol > 8:
            signals.append(f"📊 High Volatility ({vol:.1f}%)")
        if momentum > 5:
            signals.append(f"📈 Strong Momentum (+{momentum:.1f}%)")
        elif momentum < -5:
            signals.append(f"📉 Weak Momentum ({momentum:.1f}%)")
        return signals
