"""
Surge Detector — Pre-Pump Characteristic Checklist

Based on the research report "加密貨幣暴漲前徵兆研究報告" and historical data.
Monitors 3 phases of pre-surge signals and generates alerts:

Phase 1 — Capitulation Bottom (築底):
    Market is deeply oversold; "blood in the streets" conditions.
    These tell us the bottom is near but NOT that the surge has started.

Phase 2 — Smart Money Accumulation (積累):
    Institutional/smart money begins accumulating quietly.
    Price may still be flat or slightly down.

Phase 3 — Reversal Trigger (反轉觸發):
    The actual turn — multiple signals fire simultaneously.
    This is the "surge is starting" alert.

Alert levels:
    SILENCE   — no signals
    WATCH     — Phase 1 signals active (bottoming, wait)
    ACCUMULATE— Phase 1 + Phase 2 signals (smart money in, prepare)
    IMMINENT  — Phase 3 signals appearing (surge starting!)
    CONFIRMED — Multiple Phase 3 signals + resonance BULL (surge confirmed!)

Integration: Called from scan_phases._step_scan_opportunities() after
dimension scoring, before threshold adjustment.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class SurgeDetector:
    """Detects pre-surge characteristics and generates alerts."""

    def __init__(self, binance_client=None):
        self.client = binance_client

    def detect(
        self,
        dim_result: Optional[Dict] = None,
        fng: int = 50,
        fng_prev: Optional[int] = None,
        opportunities: Optional[List[Dict]] = None,
        btc_price: Optional[float] = None,
        btc_ma50: Optional[float] = None,
        btc_ma200: Optional[float] = None,
        btc_rsi: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run surge detection across all 3 phases.

        Args:
            dim_result: Output from DimensionScorer.score_all().
            fng: Current Fear & Greed index.
            fng_prev: Previous F&G reading (24h ago) for delta detection.
            opportunities: List of scan opportunities (for breadth check).
            btc_price: Current BTC price.
            btc_ma50: BTC 50-day MA.
            btc_ma200: BTC 200-day MA.
            btc_rsi: BTC daily RSI.

        Returns:
            {
                "alert_level": str,      # SILENCE/WATCH/ACCUMULATE/IMMINENT/CONFIRMED
                "phase1_signals": list,  # active capitulation signals
                "phase2_signals": list,  # active accumulation signals
                "phase3_signals": list,  # active reversal signals
                "phase1_count": int,
                "phase2_count": int,
                "phase3_count": int,
                "mvrv": Optional[float],
                "sopr": Optional[float],
                "nupl": Optional[float],
                "fng_delta": Optional[int],
                "should_alert": bool,    # True when ACCUMULATE or higher
                "summary": str,          # human-readable summary
            }
        """
        # Fetch on-chain indicators from BGeometrics
        mvrv = self._fetch_bgeometrics("/mvrv")
        sopr = self._fetch_bgeometrics("/sopr")
        nupl = self._fetch_bgeometrics("/nupl")

        # Compute F&G delta
        fng_delta = None
        if fng_prev is not None:
            fng_delta = fng - fng_prev

        # Dimension resonance
        resonance = "NEUTRAL"
        if dim_result:
            resonance = dim_result.get("resonance", "NEUTRAL")

        # ===== Phase 1: Capitulation Bottom =====
        phase1: List[str] = []

        if mvrv is not None:
            if mvrv < 1.0:
                phase1.append(f"MVRV={mvrv:.2f} — 歷史底部區（嚴重低估）")
            elif mvrv < 1.2:
                phase1.append(f"MVRV={mvrv:.2f} — 深度低估區")
            elif mvrv < 1.5:
                phase1.append(f"MVRV={mvrv:.2f} — 低於均值")

        if fng <= 20:
            phase1.append(f"F&G={fng} — 恐慌冰點")
        elif fng <= 25:
            phase1.append(f"F&G={fng} — 極度恐慌（築底條件）")

        if sopr is not None and sopr < 1.0:
            phase1.append(f"SOPR={sopr:.3f} — 持有者虧損賣出（投降）")

        if nupl is not None and nupl < 0.0:
            phase1.append(f"NUPL={nupl:.3f} — 淨未實現虧損")
        elif nupl is not None and nupl < 0.25:
            phase1.append(f"NUPL={nupl:.3f} — 恐慌-投降區間")

        if btc_rsi is not None and btc_rsi < 30:
            phase1.append(f"BTC RSI={btc_rsi:.0f} — 超賣")
        elif btc_rsi is not None and btc_rsi < 35:
            phase1.append(f"BTC RSI={btc_rsi:.0f} — 接近超賣")

        # ===== Phase 2: Smart Money Accumulation =====
        phase2: List[str] = []

        # OBV divergence from dimension signals
        if dim_result:
            tech_signals = dim_result.get("dimensions", {}).get("technical", {}).get("signals", [])
            onchain_signals = dim_result.get("dimensions", {}).get("onchain", {}).get("signals", [])
            all_signals_str = " ".join(onchain_signals + tech_signals)
            if "obv" in all_signals_str.lower() and "divergence" in all_signals_str.lower():
                phase2.append("OBV 看多背離 — 聰明錢在悄悄買入")

        # TVL stabilizing (not crashing = smart money staying)
        if dim_result:
            onchain_data = dim_result.get("dimensions", {}).get("onchain", {}).get("data", {})
            tvl_changes = onchain_data.get("chain_tvl_changes", {})
            if tvl_changes:
                avg_tvl = sum(tvl_changes.values()) / len(tvl_changes)
                if avg_tvl > -0.5 and avg_tvl < 1.0:
                    phase2.append(f"DeFi TVL 穩定（avg {avg_tvl:+.1f}%）— 資金未撤離")

        # Stablecoin supply ratio from liquidity dimension
        if dim_result:
            liq_signals = dim_result.get("dimensions", {}).get("liquidity", {}).get("signals", [])
            liq_data = dim_result.get("dimensions", {}).get("liquidity", {}).get("data", {})
            ssr = liq_data.get("ssr")
            if ssr is not None and ssr < 3.0:
                phase2.append(f"SSR={ssr:.2f} — 穩定幣購買力充裕")
            stable_growth = liq_data.get("stablecoin_growth_7d")
            if stable_growth is not None and stable_growth > 0:
                phase2.append(f"穩定幣供應7日增長{stable_growth:+.1f}%")

        # Consolidation detection from PrePump signals
        if dim_result:
            all_dim_signals = []
            for d in dim_result.get("dimensions", {}).values():
                all_dim_signals.extend(d.get("signals", []))
            dim_str = " ".join(all_dim_signals).lower()
            if "consolidation" in dim_str or "盤整" in dim_str:
                phase2.append("長期盤整中 — 積累階段")

        # MVRV recovering from bottom (smart money buying the dip)
        if mvrv is not None and mvrv > 1.0 and mvrv < 1.5:
            if sopr is not None and sopr > 0.98:
                phase2.append("MVRV+SOPR 雙確認：底部買入信號")

        # ===== Phase 3: Reversal Trigger =====
        phase3: List[str] = []

        # F&G sharp jump from extreme fear
        if fng_delta is not None and fng_delta >= 8 and fng_prev is not None and fng_prev <= 25:
            phase3.append(f"🔥 F&G 暴漲 {fng_delta}→{fng}（恐慌反轉！）")

        # Six-dimensional resonance shifting to BULL
        if resonance in ("BULL", "STRONG_BULL"):
            bullish_count = dim_result.get("bullish_count", 0) if dim_result else 0
            phase3.append(f"🔥 六維共振={resonance}（{bullish_count}/6 看漲）")
        elif resonance == "MILD_BULL":
            phase3.append(f"⚠️ 六維共振=MILD_BULL（{dim_result.get('bullish_count', 0)}/6 轉暖）")

        # BTC breaking above MA
        if btc_price and btc_ma50 and btc_price > btc_ma50:
            phase3.append("🔥 BTC 突破50日均線")
        if btc_price and btc_ma200 and btc_price > btc_ma200:
            phase3.append("🔥 BTC 突破200日均線（長期趨勢反轉）")

        # RSI crossing up from oversold
        if btc_rsi is not None and 30 <= btc_rsi < 50:
            phase3.append(f"🔥 RSI={btc_rsi:.0f} 從超賣區回升")

        # MACD bullish crossover (from technical signals)
        if dim_result:
            tech_signals_str = " ".join(
                dim_result.get("dimensions", {}).get("technical", {}).get("signals", [])
            ).lower()
            if "macd" in tech_signals_str and ("bull" in tech_signals_str or "cross" in tech_signals_str):
                phase3.append("🔥 MACD 看多交叉")

        # Breakout signals
        if dim_result:
            all_dim_signals_str = ""
            for d in dim_result.get("dimensions", {}).values():
                all_dim_signals_str += " " + " ".join(d.get("signals", []))
            all_dim_signals_str = all_dim_signals_str.lower()
            if "breaking_out" in all_dim_signals_str or "breakout" in all_dim_signals_str:
                phase3.append("🔥 盤整突破 + 量能確認")

        # Market breadth: multiple coins simultaneously scoring high
        if opportunities and len(opportunities) >= 3:
            high_score_count = sum(1 for o in opportunities if o.get("score", 0) >= 80)
            if high_score_count >= 3:
                phase3.append(f"🔥 市場廣度爆發：{high_score_count}個幣同時評分≥80")

        # ===== Determine alert level =====
        p1 = len(phase1)
        p2 = len(phase2)
        p3 = len(phase3)

        if p3 >= 3 and resonance in ("BULL", "STRONG_BULL"):
            alert_level = "CONFIRMED"
        elif p3 >= 3:
            alert_level = "IMMINENT"
        elif p3 >= 1 and (p1 + p2) >= 2:
            alert_level = "IMMINENT"
        elif p2 >= 2 and p1 >= 1:
            alert_level = "ACCUMULATE"
        elif p1 >= 2:
            alert_level = "WATCH"
        elif p3 >= 1:
            alert_level = "IMMINENT"
        else:
            alert_level = "SILENCE"

        should_alert = alert_level in ("ACCUMULATE", "IMMINENT", "CONFIRMED")

        # ===== Build summary =====
        emoji_map = {
            "SILENCE": "⚪",
            "WATCH": "🔵",
            "ACCUMULATE": "🟡",
            "IMMINENT": "🔴",
            "CONFIRMED": "🚀",
        }
        e = emoji_map.get(alert_level, "⚪")
        summary = f"{e} 暴漲預警等級: {alert_level}\n"
        if p1:
            summary += f"\n📊 Phase 1 築底信號 ({p1}):\n"
            for s in phase1:
                summary += f"  ✓ {s}\n"
        if p2:
            summary += f"\n🐋 Phase 2 積累信號 ({p2}):\n"
            for s in phase2:
                summary += f"  ✓ {s}\n"
        if p3:
            summary += f"\n🔥 Phase 3 反轉信號 ({p3}):\n"
            for s in phase3:
                summary += f"  ✓ {s}\n"
        if not (p1 or p2 or p3):
            summary += "\n無明顯暴漲前徵兆\n"

        return {
            "alert_level": alert_level,
            "phase1_signals": phase1,
            "phase2_signals": phase2,
            "phase3_signals": phase3,
            "phase1_count": p1,
            "phase2_count": p2,
            "phase3_count": p3,
            "mvrv": mvrv,
            "sopr": sopr,
            "nupl": nupl,
            "fng_delta": fng_delta,
            "resonance": resonance,
            "should_alert": should_alert,
            "summary": summary.strip(),
        }

    def _fetch_bgeometrics(self, endpoint: str) -> Optional[float]:
        """Fetch latest value from BGeometrics API.

        Free tier: 10 req/h, 15 req/day.
        surge_detector.detect() calls this 3 times (mvrv/sopr/nupl).
        Combined with DimensionScorer._fetch_mvrv(), that's 4 calls/scan.
        At max 12 scans/day → 48 calls/day, within 15*31=465 monthly budget.

        Args:
            endpoint: One of "/mvrv", "/sopr", "/nupl".

        Returns:
            Latest float value or None.
        """
        api_key = os.environ.get("BGEOMETRICS_API_KEY", "")
        if not api_key:
            return None

        try:
            url = f"https://api.bitcoin-data.com/v1{endpoint}"
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"startday": "today", "endday": "today"},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.debug(f"BGeometrics {endpoint} returned {resp.status_code}")
                return None

            data = resp.json()
            if isinstance(data, list) and data:
                # Extract the value from the most recent entry
                # Response format: [{"d": "2026-07-09", "mvrv": 1.2019, ...}]
                latest = data[-1]
                # The value key matches the endpoint name
                val_key = endpoint.strip("/")
                val = latest.get(val_key)
                if val is not None:
                    return float(val)
            return None
        except Exception as e:
            logger.debug(f"BGeometrics {endpoint} fetch failed: {e}")
            return None
