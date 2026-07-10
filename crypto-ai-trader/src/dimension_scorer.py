"""
Dimension Scorer — Six-Dimension Resonance Framework

Based on research report "加密貨幣暴漲前徵兆研究報告":
- 4+ dimensions same-direction resonance → 90d surge probability >72%
- 5 dimensions → 85%, 6 dimensions → 92%

Dimensions (weight):
  1. On-Chain (25%)    — Chain TVL (DeFiLlama) + MVRV (BGeometrics) + BTC volume
  2. Liquidity (25%)   — funding rate, stablecoin supply (DeFiLlama), DEX volume
  3. Macro (20%)       — BTC trend, F&G regime
  4. Sentiment (15%)   — CFGI persistence, fear/greed
  5. Technical (10%)   — RSI, MACD, volume
  6. Regulatory (5%)   — news sentiment

v2 (2026-06-22): Integrated llama-data-skill for DeFiLlama on-chain data.
D1 now uses real chain TVL data instead of BTC volume proxy.
D2 now includes stablecoin supply trends alongside funding rate.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

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
        weighted = sum(d["score"] * d["weight"] for d in dims.values())

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
    def _fetch_mvrv(self) -> Optional[float]:
        """Fetch latest MVRV ratio from BGeometrics API.

        Free tier: 10 req/h, 15 req/day. We call once per scan (2-4h interval).
        Returns latest MVRV float or None on failure.
        """
        api_key = os.environ.get("BGEOMETRICS_API_KEY", "")
        if not api_key:
            logger.debug("BGEOMETRICS_API_KEY not set, skipping MVRV")
            return None

        try:
            url = "https://api.bitcoin-data.com/v1/mvrv"
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"startday": "today", "endday": "today"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return float(data[-1].get("mvrv", 0))
            logger.warning("MVRV API returned empty or unexpected format")
            return None
        except Exception as e:
            logger.warning(f"MVRV fetch failed: {e}")
            return None

    # ------------------------------------------------------------------
    def _score_onchain(self) -> Dict:
        """D1: On-Chain — DeFiLlama chain TVL + MVRV + BTC volume.

        Uses llama-data-skill for real on-chain TVL data (chain-level),
        falls back to BTC volume proxy if llama unavailable.
        """
        score = 0.0
        signals = []
        data: Dict[str, Any] = {}

        # --- Primary: DeFiLlama chain TVL via llama-data-skill ---
        try:
            from src.data_feed_llama import LlamaDataFeed

            llama = LlamaDataFeed()
            chain_tvl = llama.get_chain_tvl()

            if chain_tvl:
                data["chain_tvl_changes"] = chain_tvl
                avg_tvl_change = sum(chain_tvl.values()) / len(chain_tvl)
                chains_up = sum(1 for v in chain_tvl.values() if v > 0.5)
                chains_down = sum(1 for v in chain_tvl.values() if v < -0.5)

                # Aggregate TVL direction is a strong on-chain signal
                if avg_tvl_change > 2:
                    score += 0.4
                    signals.append(f"tvl_strong_inflow_{avg_tvl_change:+.1f}pct")
                elif avg_tvl_change > 0.5:
                    score += 0.2
                    signals.append(f"tvl_inflow_{avg_tvl_change:+.1f}pct")
                elif avg_tvl_change < -2:
                    score -= 0.4
                    signals.append(f"tvl_strong_outflow_{avg_tvl_change:+.1f}pct")
                elif avg_tvl_change < -0.5:
                    score -= 0.2
                    signals.append(f"tvl_outflow_{avg_tvl_change:+.1f}pct")
                else:
                    signals.append(f"tvl_flat_{avg_tvl_change:+.1f}pct")

                # Cross-check: divergence between chains
                if chains_up >= 3 and chains_down == 0:
                    score += 0.1
                    signals.append(f"tvl_broad_inflow_{chains_up}chains")
                elif chains_down >= 3 and chains_up == 0:
                    score -= 0.1
                    signals.append(f"tvl_broad_outflow_{chains_down}chains")

            else:
                # Fallback to old DeFiLlama direct API
                logger.debug("Llama TVL unavailable, falling back to direct API")
                try:
                    from src.data_feed_onchain import DeFiLlamaOnChain

                    onchain = DeFiLlamaOnChain()
                    old_changes = onchain.get_chain_tvl_changes()
                    if old_changes:
                        data["chain_tvl_changes_fallback"] = old_changes
                        avg_chg = sum(old_changes.values()) / len(old_changes)
                        if avg_chg > 1:
                            score += 0.2
                            signals.append(f"tvl_fallback_inflow_{avg_chg:+.1f}pct")
                        elif avg_chg < -1:
                            score -= 0.2
                            signals.append(f"tvl_fallback_outflow_{avg_chg:+.1f}pct")
                except Exception as e:
                    logger.warning("dimension_scorer._score_onchain: " + str(e))
                    pass

        except Exception as e:
            logger.warning(f"DeFiLlama on-chain scoring failed: {e}")

        # --- MVRV from BGeometrics (on-chain valuation, no Binance needed) ---
        # MVRV < 1.0: Historical bottom (strong buy), < 1.5: undervalued
        # MVRV 1.5-3.0: fair value, > 3.7: market top (strong sell)
        mvrv = self._fetch_mvrv()
        if mvrv is not None and mvrv > 0:
            data["mvrv"] = mvrv
            if mvrv < 1.0:
                score += 0.4
                signals.append(f"mvrv_bottom_{mvrv:.2f}")
            elif mvrv < 1.2:
                score += 0.25
                signals.append(f"mvrv_undervalued_{mvrv:.2f}")
            elif mvrv < 1.5:
                score += 0.1
                signals.append(f"mvrv_below_avg_{mvrv:.2f}")
            elif mvrv > 3.7:
                score -= 0.4
                signals.append(f"mvrv_top_{mvrv:.2f}")
            elif mvrv > 3.0:
                score -= 0.2
                signals.append(f"mvrv_overvalued_{mvrv:.2f}")
            # else: 1.5-3.0 = neutral, no signal

        # --- Backup: BTC volume from Binance (always available) ---
        if not self.client:
            if not signals:
                return {"score": 0, "signals": ["no_client"], "weight": 0.25, "data": data}
            return {"score": max(-1, min(1, score)), "signals": signals, "weight": 0.25, "data": data}

        try:
            stats = self.client.get_24hr_stats("BTCUSDT")
            if stats:
                vol = float(stats.get("quote_volume", 0))
                price_change = float(stats.get("price_change_pct", 0))
                data["btc_volume_24h"] = vol
                data["btc_price_change"] = price_change

                # BTC volume as secondary signal (smaller weight than TVL)
                if vol > 5_000_000_000 and price_change > 1.5:
                    score += 0.15
                    signals.append("BTC_high_vol_accumulation")
                elif vol > 5_000_000_000 and price_change < -1.5:
                    score -= 0.15
                    signals.append("BTC_high_vol_distribution")
        except Exception as e:
            logger.warning(f"BTC volume scoring failed: {e}")

        return {"score": max(-1, min(1, score)), "signals": signals, "weight": 0.25, "data": data}

    # ------------------------------------------------------------------
    def _score_liquidity(self) -> Dict:
        """D2: Liquidity — funding rate + stablecoin supply + DEX volume.

        Combines three liquidity signals:
        1. Funding rate 30d rolling avg (existing)
        2. Stablecoin supply trends via DeFiLlama (new: capital inflow/outflow)
        3. DEX volume trend via DeFiLlama (new: market activity)
        """
        score = 0.0
        signals = []
        data: Dict[str, Any] = {}

        # --- Signal 1: Funding rate (existing, 50% of liquidity weight) ---
        funding_score = 0.0
        try:
            from src.data_feed_funding import FundingRate

            fr = FundingRate()
            btc_fr = fr.get_funding_rolling_avg("BTCUSDT", days=30)
            data["funding"] = btc_fr

            strength = btc_fr.get("signal_strength", 0)
            signal = btc_fr.get("signal", "NEUTRAL")
            funding_score = strength / 5.0
            signals.append(f"funding_30d:{signal}")
            signals.append(f"funding_avg:{btc_fr.get('rolling_avg', 0):.6f}")

            if btc_fr.get("negative_pct", 0) > 70:
                funding_score += 0.2
                signals.append(f"funding_neg_{btc_fr['negative_pct']:.0f}pct")

        except Exception as e:
            logger.warning(f"Funding rate scoring failed: {e}")

        score += funding_score * 0.5  # 50% weight for funding

        # --- Signal 2: Stablecoin supply via llama-data-skill (new, 30% of liquidity) ---
        stbl_score = 0.0
        try:
            from src.data_feed_llama import LlamaDataFeed

            llama = LlamaDataFeed()
            stbl = llama.get_stablecoin_supply()

            if stbl:
                data["stablecoin"] = stbl
                usdt = stbl.get("usdt_circulating", 0)
                usdc = stbl.get("usdc_circulating", 0)
                total = stbl.get("total_circulating_usd", 0)
                usdt_chg = stbl.get("usdt_change_day", 0)
                usdc_chg = stbl.get("usdc_change_day", 0)

                signals.append(f"stbl_total_${total/1e9:.0f}B")
                if usdt > 0:
                    signals.append(f"USDT_${usdt/1e9:.1f}B_{usdt_chg:+.2f}pct")
                if usdc > 0:
                    signals.append(f"USDC_${usdc/1e9:.1f}B_{usdc_chg:+.2f}pct")

                # --- Change-rate component (60% of stablecoin score) ---
                avg_stbl_chg = (usdt_chg + usdc_chg) / 2 if (usdt_chg or usdc_chg) else 0
                if avg_stbl_chg > 0.5:
                    stbl_change_score = 0.3
                    signals.append(f"stbl_capital_inflow_{avg_stbl_chg:+.1f}pct")
                elif avg_stbl_chg > 0.1:
                    stbl_change_score = 0.15
                    signals.append(f"stbl_mild_inflow_{avg_stbl_chg:+.1f}pct")
                elif avg_stbl_chg < -0.5:
                    stbl_change_score = -0.3
                    signals.append(f"stbl_capital_outflow_{avg_stbl_chg:+.1f}pct")
                elif avg_stbl_chg < -0.1:
                    stbl_change_score = -0.15
                    signals.append(f"stbl_mild_outflow_{avg_stbl_chg:+.1f}pct")
                else:
                    stbl_change_score = 0.0

                # Depeg alerts are a strong bearish signal
                depegs = stbl.get("depeg_alerts", [])
                if depegs:
                    for d in depegs:
                        stbl_change_score -= 0.3
                        signals.append(f"depeg_{d.get('symbol','?')}_{d['deviation_pct']:+.1f}pct")
                    data["depeg_alerts"] = depegs

                # --- SSR (Stablecoin Supply Ratio) component (40% of stablecoin score) ---
                # SSR = BTC market cap / total stablecoin supply
                # Low SSR (< 10) = strong stablecoin purchasing power (bullish)
                # High SSR (> 15) = weak purchasing power (bearish)
                ssr_score = 0.0
                try:
                    if self.client and total > 0:
                        btc_price = self.client.get_ticker_price("BTCUSDT")
                        if btc_price and btc_price > 0:
                            btc_mcap = btc_price * 19_700_000
                            ssr = btc_mcap / total
                            data["ssr"] = round(ssr, 2)
                            signals.append(f"SSR_{ssr:.1f}")
                            if ssr < 10:
                                ssr_score = 0.3
                                signals.append(f"SSR_low_bullish_{ssr:.1f}")
                            elif ssr > 15:
                                ssr_score = -0.3
                                signals.append(f"SSR_high_bearish_{ssr:.1f}")
                except Exception as e:
                    logger.warning(f"SSR calculation failed: {e}")

                # Combine: change rate 60% + SSR 40%
                stbl_score = stbl_change_score * 0.6 + ssr_score * 0.4

        except Exception as e:
            logger.warning(f"Stablecoin scoring failed: {e}")

        score += stbl_score * 0.3  # 30% weight for stablecoin

        # --- Signal 3: DEX volume via llama-data-skill (new, 20% of liquidity) ---
        dex_score = 0.0
        try:
            if "llama" not in dir():
                from src.data_feed_llama import LlamaDataFeed
                llama = LlamaDataFeed()

            dex = llama.get_dex_volume()

            if dex:
                data["dex_volume"] = dex
                vol_24h = dex.get("total_24h_usd", 0)
                change_pct = dex.get("change_24h_pct", 0)

                if vol_24h > 0:
                    signals.append(f"dex_vol_${vol_24h/1e9:.1f}B")

                # Volume spike = high activity, could precede moves
                if change_pct > 30:
                    dex_score += 0.3
                    signals.append(f"dex_vol_spike_{change_pct:+.0f}pct")
                elif change_pct > 10:
                    dex_score += 0.15
                    signals.append(f"dex_vol_up_{change_pct:+.0f}pct")
                elif change_pct < -30:
                    dex_score -= 0.3
                    signals.append(f"dex_vol_collapse_{change_pct:+.0f}pct")
                elif change_pct < -10:
                    dex_score -= 0.15
                    signals.append(f"dex_vol_down_{change_pct:+.0f}pct")

                # Record top DEXes for debugging
                top = dex.get("top_dexes", [])
                if top:
                    data["top_dexes"] = [f"{d['name']}:${d['volume_24h']/1e9:.1f}B" for d in top[:3]]

        except Exception as e:
            logger.warning(f"DEX volume scoring failed: {e}")

        score += dex_score * 0.2  # 20% weight for DEX volume

        return {
            "score": max(-1, min(1, score)),
            "signals": signals,
            "weight": 0.25,
            "data": data,
        }

    # ------------------------------------------------------------------
    def _score_macro(self) -> Dict:
        """D3: Macro — BTC trend strength + market regime."""
        score = 0.0
        signals = []
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
                else:
                    signals.append(f"BTC_neutral_{change:+.1f}pct")
        except Exception as e:
            logger.warning(f"Macro scoring failed: {e}")

        return {"score": score, "signals": signals, "weight": 0.20, "data": data}

    # ------------------------------------------------------------------
    def _score_sentiment(self) -> Dict:
        """D4: Sentiment — CFGI persistence tracking."""
        score = 0.0
        signals = []
        data: Dict[str, Any] = {}

        try:
            from src.sentiment import SentimentAnalyzer

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
            logger.warning(f"Sentiment scoring failed: {e}")

        return {"score": score, "signals": signals, "weight": 0.15, "data": data}

    # ------------------------------------------------------------------
    def _score_technical(self) -> Dict:
        """D5: Technical — RSI + Seller Exhaustion from 1h candles.

        Combines two sub-signals (each 50% weight):
        1. RSI (Wilder's smoothing, 1h, 50 candles)
        2. Seller Exhaustion Constant (drawdown / volatility)
        """
        score = 0.0
        signals = []
        data: Dict[str, Any] = {}

        if not self.client:
            return {"score": 0, "signals": ["no_client"], "weight": 0.10, "data": data}

        try:
            # Fetch 1h klines — use 50 candles to match scanner's timeframe and reduce noise
            klines = self.client.get_klines(symbol="BTCUSDT", interval="1h", limit=50)
            closes = [float(k["close"]) for k in klines]
            if len(closes) >= 15:
                # Wilder's smoothing RSI (consistent with indicators.py)
                gains = []
                losses = []
                for i in range(1, len(closes)):
                    diff = closes[i] - closes[i - 1]
                    gains.append(max(0, diff))
                    losses.append(max(0, -diff))
                # Seed with simple average of first 14 values
                avg_gain = sum(gains[:14]) / 14
                avg_loss = sum(losses[:14]) / 14
                # Wilder's EMA for the rest
                for i in range(14, len(gains)):
                    avg_gain = (avg_gain * 13 + gains[i]) / 14
                    avg_loss = (avg_loss * 13 + losses[i]) / 14
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                else:
                    rsi = 100
                data["rsi_1h"] = round(rsi, 1)

                rsi_score = 0.0
                if rsi < 30:
                    rsi_score = 0.4
                    signals.append(f"RSI_oversold_{rsi:.0f}")
                elif rsi < 40:
                    rsi_score = 0.2
                    signals.append(f"RSI_low_{rsi:.0f}")
                elif rsi > 70:
                    rsi_score = -0.4
                    signals.append(f"RSI_overbought_{rsi:.0f}")
                elif rsi > 60:
                    rsi_score = -0.1
                    signals.append(f"RSI_high_{rsi:.0f}")
                else:
                    signals.append(f"RSI_neutral_{rsi:.0f}")

                # --- Seller Exhaustion Constant ---
                # drawdown_pct = (max_close - current) / max_close
                # cv = std(closes) / mean(closes)
                # exhaustion = drawdown_pct / (cv + 0.001)
                # High exhaustion (big drop + low vol) = sellers exhausted = bullish reversal
                exhaustion_score = 0.0
                try:
                    max_close = max(closes)
                    current_close = closes[-1]
                    drawdown_pct = (max_close - current_close) / max_close if max_close > 0 else 0
                    mean_close = sum(closes) / len(closes)
                    variance = sum((c - mean_close) ** 2 for c in closes) / len(closes)
                    std_close = variance ** 0.5
                    cv = std_close / mean_close if mean_close > 0 else 0
                    exhaustion = drawdown_pct / (cv + 0.001)
                    data["exhaustion"] = round(exhaustion, 3)
                    data["drawdown_pct"] = round(drawdown_pct * 100, 2)
                    signals.append(f"exhaustion_{exhaustion:.2f}")
                    if exhaustion > 5:
                        exhaustion_score = 0.3
                        signals.append(f"exhaustion_high_bullish_{exhaustion:.2f}")
                    elif exhaustion > 3:
                        exhaustion_score = 0.15
                        signals.append(f"exhaustion_mild_{exhaustion:.2f}")
                except Exception as e:
                    logger.warning(f"Seller exhaustion calculation failed: {e}")

                # RSI and exhaustion each contribute 50% to D5 score
                score = rsi_score * 0.5 + exhaustion_score * 0.5

        except Exception as e:
            logger.warning(f"Technical scoring failed: {e}")

        return {"score": score, "signals": signals, "weight": 0.10, "data": data}

    # ------------------------------------------------------------------
    def _score_regulatory(self) -> Dict:
        """D6: Market Sentiment (misnamed "Regulatory" for legacy compat).

        Uses BTC volume + price change as a risk-on/off proxy:
        - BTC drops >2% with high volume → risk-off / panic
        - BTC gains >2% → risk-on momentum

        NOTE: This does NOT measure actual regulatory events (no news API).
        Weight kept low (5%) to limit impact of this crude proxy.
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
                        return {
                            "score": -0.3,
                            "signals": ["risk_off_high_volume"],
                            "weight": 0.05,
                            "data": {"btc_change": btc_change},
                        }
                    elif btc_change > 2 and btc_vol > 0:
                        return {
                            "score": 0.3,
                            "signals": ["risk_on_momentum"],
                            "weight": 0.05,
                            "data": {"btc_change": btc_change},
                        }
        except Exception as e:
            logger.warning(f"DimensionScorer: regulatory dimension error: {e}")
        return {
            "score": 0.0,
            "signals": ["neutral_regulatory"],
            "weight": 0.05,
            "data": {},
        }

    # ------------------------------------------------------------------
    def format_report(self, result: Dict) -> str:
        """Format dimension scoring result for display."""
        lines = []
        lines.append("=== 六維度共振分析 ===")
        lines.append(
            f"共振狀態: {result['resonance']} | 暴漲概率: {result['surge_probability']}"
        )
        lines.append(
            f"看漲維度: {result['bullish_count']}/6 | 看跌維度: {result['bearish_count']}/6 | 加權分: {result['weighted_score']:+.3f}"
        )
        lines.append("")

        for name, dim in result["dimensions"].items():
            score = dim["score"]
            weight = dim["weight"]
            icon = "🟢" if score > 0.2 else "🔴" if score < -0.2 else "⚪"
            signals_str = ", ".join(dim.get("signals", []))
            lines.append(
                f"{icon} {name:12} ({weight*100:.0f}%) score={score:+.2f} | {signals_str}"
            )

        return "\n".join(lines)
