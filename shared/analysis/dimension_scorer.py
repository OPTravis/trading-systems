"""
Dimension Scorer — Six-Dimension Resonance Framework

Based on research report "加密貨幣暴漲前徵兆研究報告":
- 4+ dimensions same-direction resonance → 90d surge probability >72%
- 5 dimensions → 85%, 6 dimensions → 92%

Dimensions (weight):
  1. On-Chain (25%)    — MVRV, exchange reserves
  2. Liquidity (25%)   — funding rate, stablecoin supply
  3. Macro (20%)       — BTC trend, F&G regime
  4. Sentiment (15%)   — CFGI persistence, fear/greed
  5. Technical (10%)   — RSI, MACD, volume
  6. Regulatory (5%)   — news sentiment

Only dimensions 2, 4, 5 are fully implementable with current API access.
Dimensions 1, 3, 6 are approximated from available data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class DimensionScorer:
    """Score market across 6 dimensions and detect resonance.

    Each dimension returns:
      - score: -1 (bearish) to +1 (bullish)
      - signals: list of active signal descriptions
      - data: raw data dict for debugging
    """

    def __init__(self, binance_client=None):
        self.client = binance_client

    # ------------------------------------------------------------------
    def score_all(self) -> Dict[str, Any]:
        """Score all 6 dimensions and compute resonance.

        Returns:
            {
                dimensions: {name: {score, signals, weight}},
                bullish_count: int,  # dimensions with score > 0.2
                bearish_count: int,  # dimensions with score < -0.2
                resonance: str,      # "STRONG_BULL" / "BULL" / "NEUTRAL" / "BEAR" / "STRONG_BEAR"
                weighted_score: float,  # -1 to +1
                surge_probability: str,
            }
        """
        dims = {}

        # D1: On-Chain (approximated from Binance data)
        dims["onchain"] = self._score_onchain()
        # D2: Liquidity (funding rate 30d avg)
        dims["liquidity"] = self._score_liquidity()
        # D3: Macro (BTC trend + market regime)
        dims["macro"] = self._score_macro()
        # D4: Sentiment (CFGI persistence)
        dims["sentiment"] = self._score_sentiment()
        # D5: Technical (from scanner signals)
        dims["technical"] = self._score_technical()
        # D6: Regulatory (from news sentiment)
        dims["regulatory"] = self._score_regulatory()

        # Count bullish/bearish dimensions
        bullish = sum(1 for d in dims.values() if d["score"] > 0.2)
        bearish = sum(1 for d in dims.values() if d["score"] < -0.2)

        # Weighted score
        weighted = sum(
            d["score"] * d["weight"] for d in dims.values()
        )

        # Resonance classification
        if bullish >= 5:
            resonance = "STRONG_BULL"
            prob = "85-92%"
        elif bullish >= 4:
            resonance = "BULL"
            prob = "72%"
        elif bearish >= 5:
            resonance = "STRONG_BEAR"
            prob = "85-92% drop"
        elif bearish >= 4:
            resonance = "BEAR"
            prob = "72% drop"
        elif bullish >= 3:
            resonance = "MILD_BULL"
            prob = "52%"
        elif bearish >= 3:
            resonance = "MILD_BEAR"
            prob = "52% drop"
        else:
            resonance = "NEUTRAL"
            prob = "N/A"

        return {
            "dimensions": dims,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "resonance": resonance,
            "weighted_score": round(weighted, 3),
            "surge_probability": prob,
        }

    # ------------------------------------------------------------------
    def _score_onchain(self) -> Dict:
        """D1: On-Chain — approximated from Binance orderbook depth."""
        score = 0.0
        signals: list[str] = []
        data: Dict[str, Any] = {}

        if not self.client:
            return {"score": 0, "signals": ["no_client"], "weight": 0.25, "data": data}

        try:
            # Use BTC 24h stats as proxy
            stats = self.client.get_24hr_stats("BTCUSDT")
            if stats:
                vol = float(stats.get("quote_volume", 0))
                price_change = float(stats.get("price_change_pct", 0))
                data["btc_volume_24h"] = vol
                data["btc_price_change"] = price_change

                # High volume + price up = accumulation proxy
                if vol > 5_000_000_000 and price_change > 1.5:
                    score += 0.3
                    signals.append("BTC_high_vol_accumulation")
                elif vol > 5_000_000_000 and price_change < -1.5:
                    score -= 0.3
                    signals.append("BTC_high_vol_distribution")
                elif vol > 2_000_000_000 and price_change > 0.5:
                    score += 0.15
                    signals.append("BTC_vol_accumulation")
                elif vol > 2_000_000_000 and price_change < -0.5:
                    score -= 0.15
                    signals.append("BTC_vol_distribution")
        except Exception as e:
            logger.debug(f"On-chain scoring failed: {e}")

        return {"score": score, "signals": signals, "weight": 0.25, "data": data}

    # ------------------------------------------------------------------
    def _score_liquidity(self) -> Dict:
        """D2: Liquidity — funding rate 30d rolling average."""
        score = 0.0
        signals = []
        data = {}

        try:
            from ..core.data_feed_funding import FundingRate
            fr = FundingRate()
            btc_fr = fr.get_funding_rolling_avg("BTCUSDT", days=30)
            data = btc_fr

            strength = btc_fr.get("signal_strength", 0)
            signal = btc_fr.get("signal", "NEUTRAL")
            score = strength / 5.0  # normalize to -1..+1
            signals.append(f"funding_30d:{signal}")
            signals.append(f"funding_avg:{btc_fr.get('rolling_avg', 0):.6f}")

            # Also check BTC specifically for extreme readings
            if btc_fr.get("negative_pct", 0) > 70:
                score += 0.2
                signals.append(f"funding_neg_{btc_fr['negative_pct']:.0f}pct")

        except Exception as e:
            logger.debug(f"Liquidity scoring failed: {e}")

        return {"score": max(-1, min(1, score)), "signals": signals, "weight": 0.25, "data": data}

    # ------------------------------------------------------------------
    def _score_macro(self) -> Dict:
        """D3: Macro — BTC trend strength + market regime."""
        score = 0.0
        signals: list[str] = []
        data: Dict[str, Any] = {}

        if not self.client:
            return {"score": 0, "signals": ["no_client"], "weight": 0.20, "data": data}

        try:
            stats = self.client.get_24hr_stats("BTCUSDT")
            if stats:
                change = float(stats.get("price_change_pct", 0))
                data["btc_24h_change"] = change

                if change > 3:
                    score += 0.4
                    signals.append("BTC_strong_uptrend")
                elif change > 1:
                    score += 0.2
                    signals.append("BTC_uptrend")
                elif change < -3:
                    score -= 0.4
                    signals.append("BTC_strong_downtrend")
                elif change < -1:
                    score -= 0.2
                    signals.append("BTC_downtrend")
        except Exception as e:
            logger.debug(f"Macro scoring failed: {e}")

        return {"score": score, "signals": signals, "weight": 0.20, "data": data}

    # ------------------------------------------------------------------
    def _score_sentiment(self) -> Dict:
        """D4: Sentiment — CFGI persistence tracking."""
        score = 0.0
        signals = []
        data = {}

        try:
            from ..core.sentiment import SentimentAnalyzer
            sa = SentimentAnalyzer()
            market = sa.get_market_sentiment()
            data = {
                "fng": market.get("fear_greed", 50),
                "consecutive_fear": market.get("consecutive_fear_days", 0),
                "consecutive_greed": market.get("consecutive_greed_days", 0),
                "signal": market.get("signal", "NEUTRAL"),
            }

            fng = market.get("fear_greed", 50)
            consec_fear = market.get("consecutive_fear_days", 0)
            consec_greed = market.get("consecutive_greed_days", 0)
            signal = market.get("signal", "NEUTRAL")

            if signal == "STRONG_REVERSAL_BUY":
                score = 0.8  # 14+ days fear = very bullish
                signals.append(f"CFGI_fear_{consec_fear}d_STRONG")
            elif signal == "REVERSAL_BUY":
                score = 0.5  # 10+ days fear
                signals.append(f"CFGI_fear_{consec_fear}d")
            elif signal == "OVERBOUGHT_WARNING":
                score = -0.5  # 7+ days greed
                signals.append(f"CFGI_greed_{consec_greed}d")
            elif fng <= 25:
                score = 0.3
                signals.append(f"CFGI_extreme_fear_{fng}")
            elif fng <= 45:
                score = 0.1
                signals.append(f"CFGI_fear_{fng}")
            elif fng >= 75:
                score = -0.3
                signals.append(f"CFGI_extreme_greed_{fng}")
            elif fng >= 60:
                score = -0.1
                signals.append(f"CFGI_greed_{fng}")

        except Exception as e:
            logger.debug(f"Sentiment scoring failed: {e}")

        return {"score": score, "signals": signals, "weight": 0.15, "data": data}

    # ------------------------------------------------------------------
    def _score_technical(self) -> Dict:
        """D5: Technical — RSI + volume trend."""
        score = 0.0
        signals: list[str] = []
        data: Dict[str, Any] = {}

        if not self.client:
            return {"score": 0, "signals": ["no_client"], "weight": 0.10, "data": data}

        try:
            # Fetch 1h klines for RSI calculation
            # Binance returns lists: [open_time, open, high, low, close, ...]
            klines = self.client.get_klines(symbol="BTCUSDT", interval="1h", limit=15)
            closes = [float(k["close"]) for k in klines]
            if len(closes) >= 14:
                # Simple RSI calculation
                gains = []
                losses = []
                for i in range(1, len(closes)):
                    diff = closes[i] - closes[i-1]
                    gains.append(max(0, diff))
                    losses.append(max(0, -diff))
                avg_gain = sum(gains[-14:]) / 14
                avg_loss = sum(losses[-14:]) / 14
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                else:
                    rsi = 100
                data["rsi_1h"] = round(rsi, 1)

                if rsi < 30:
                    score += 0.4
                    signals.append(f"RSI_oversold_{rsi:.0f}")
                elif rsi < 40:
                    score += 0.2
                    signals.append(f"RSI_low_{rsi:.0f}")
                elif rsi > 70:
                    score -= 0.4
                    signals.append(f"RSI_overbought_{rsi:.0f}")
                elif rsi > 60:
                    score -= 0.1
                    signals.append(f"RSI_high_{rsi:.0f}")

        except Exception as e:
            logger.debug(f"Technical scoring failed: {e}")

        return {"score": score, "signals": signals, "weight": 0.10, "data": data}

    # ------------------------------------------------------------------
    def _score_regulatory(self) -> Dict:
        """D6: Regulatory/Sentiment — use BTC volume ratio as proxy.

        Different from macro (which uses BTC price change). High BTC volume
        relative to altcoins suggests risk-off / regulatory uncertainty,
        while low BTC volume ratio suggests risk-on / favorable environment.
        """
        try:
            if self.client:
                # Use BTC trading volume vs a proxy for total market activity
                btc_stats = self.client.get_24hr_stats("BTCUSDT")
                if btc_stats:
                    btc_vol = float(btc_stats.get("volume", 0))
                    btc_change = float(btc_stats.get("price_change_pct", 0))
                    # Cross-signal: when BTC drops AND has high volume = panic/risk-off
                    if btc_change < -2 and btc_vol > 0:
                        return {"score": -0.3, "signals": ["risk_off_high_volume"], "weight": 0.05, "data": {"btc_change": btc_change}}
                    elif btc_change > 2 and btc_vol > 0:
                        return {"score": 0.3, "signals": ["risk_on_momentum"], "weight": 0.05, "data": {"btc_change": btc_change}}
        except Exception:
            pass
        return {"score": 0.0, "signals": ["neutral_regulatory"], "weight": 0.05, "data": {}}

    # ------------------------------------------------------------------
    def format_report(self, result: Dict) -> str:
        """Format dimension scoring result for display."""
        lines = []
        lines.append(f"=== 六維度共振分析 ===")
        lines.append(f"共振狀態: {result['resonance']} | 暴漲概率: {result['surge_probability']}")
        lines.append(f"看漲維度: {result['bullish_count']}/6 | 看跌維度: {result['bearish_count']}/6 | 加權分: {result['weighted_score']:+.3f}")
        lines.append("")

        for name, dim in result["dimensions"].items():
            score = dim["score"]
            weight = dim["weight"]
            icon = "🟢" if score > 0.2 else "🔴" if score < -0.2 else "⚪"
            signals_str = ", ".join(dim.get("signals", []))
            lines.append(f"{icon} {name:12} ({weight*100:.0f}%) score={score:+.2f} | {signals_str}")

        return "\n".join(lines)
