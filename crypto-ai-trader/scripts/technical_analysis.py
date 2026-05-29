#!/usr/bin/env python3
"""
Technical Analysis Report for Candidate Coins
==============================================
Analyzes: K-line patterns, volume changes, support/resistance, RSI/MACD,
          24h volatility, funding rates.

Output: data/technical_analysis_YYYY-MM-DD.md
"""

import sys
import os
import time
import math
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.binance_client import BinanceClient
from src.indicators import Indicators

# ── Configuration ──────────────────────────────────────────────────────────

# Current holdings
HOLDINGS = ["TAOUSDT", "PENDLEUSDT", "AVAXUSDT"]

# Candidate coins to scan (top alts + momentum plays)
SCAN_CANDIDATES = [
    "SOLUSDT", "DOGEUSDT", "XRPUSDT", "SUIUSDT", "SEIUSDT",
    "NEARUSDT", "ENAUSDT", "LDOSUSDT", "MOVRUSDT", "HIGHUSDT",
]

# Timeframes for analysis
TIMEFRAMES = {
    "1d":  {"interval": "1d",  "limit": 90},
    "4h":  {"interval": "4h",  "limit": 100},
    "1h":  {"interval": "1h",  "limit": 100},
    "15m": {"interval": "15m", "limit": 200},
}

# Rate limiter
_last_api_call = 0.0
API_DELAY = 0.12  # 120ms between calls


def rate_limited_sleep():
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < API_DELAY:
        time.sleep(API_DELAY - elapsed)
    _last_api_call = time.time()


# ── K-line Pattern Detection ──────────────────────────────────────────────

def detect_patterns(klines: List[Dict]) -> List[str]:
    """Detect candlestick patterns from kline data."""
    if len(klines) < 3:
        return []

    patterns = []
    recent = klines[-5:]  # Check last 5 candles

    for i, k in enumerate(recent):
        o, h, l, c = k["open"], k["high"], k["low"], k["close"]
        body = abs(c - o)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        total_range = h - l

        if total_range == 0:
            continue

        body_ratio = body / total_range
        upper_ratio = upper_wick / total_range
        lower_ratio = lower_wick / total_range

        # Doji: tiny body
        if body_ratio < 0.1 and total_range > 0:
            patterns.append("Doji (猶豫)")

        # Hammer / Hanging Man
        if lower_ratio > 0.6 and body_ratio < 0.25:
            if c > o:
                patterns.append("Hammer (錘子線) — 看漲反轉")
            else:
                patterns.append("Hanging Man (上吊線) — 看跌反轉")

        # Shooting Star
        if upper_ratio > 0.6 and body_ratio < 0.25 and c < o:
            patterns.append("Shooting Star (射擊之星) — 看跌反轉")

        # Engulfing pattern
        if i > 0:
            prev = recent[i-1]
            prev_o, prev_c = prev["open"], prev["close"]
            if prev_c < prev_o and c > o and o <= prev_c and c >= prev_o:
                patterns.append("Bullish Engulfing (看漲吞沒)")
            elif prev_c > prev_o and c < o and o >= prev_c and c <= prev_o:
                patterns.append("Bearish Engulfing (看跌吞沒)")

        # Three white soldiers / Three black crows
        if i >= 2:
            c1, c2, c3 = recent[i-2]["close"], recent[i-1]["close"], k["close"]
            o1, o2, o3 = recent[i-2]["open"], recent[i-1]["open"], k["open"]
            if c1 > o1 and c2 > o2 and c3 > o3 and c3 > c2 > c1:
                patterns.append("Three White Soldiers (三白兵) — 強烈看漲")
            elif c1 < o1 and c2 < o2 and c3 < o3 and c3 < c2 < c1:
                patterns.append("Three Black Crows (三黑鴉) — 強烈看跌")

    # Deduplicate
    seen = set()
    unique = []
    for p in patterns:
        key = p.split("—")[0].strip().split("(")[0].strip()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


# ── Support & Resistance ──────────────────────────────────────────────────

def find_support_resistance(klines: List[Dict], lookback: int = 60) -> Dict:
    """Find key support/resistance levels using pivot points and volume profile."""
    if len(klines) < lookback:
        lookback = len(klines)

    data = klines[-lookback:]
    highs = [k["high"] for k in data]
    lows = [k["low"] for k in data]
    closes = [k["close"] for k in data]
    volumes = [k["volume"] for k in data]

    # Pivot point method
    pivots_high = []
    pivots_low = []
    window = 5

    for i in range(window, len(data) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            pivots_high.append((i, highs[i]))
        if lows[i] == min(lows[i-window:i+window+1]):
            pivots_low.append((i, lows[i]))

    # Cluster nearby pivots (within 1.5% of each other)
    def cluster_levels(levels, threshold_pct=1.5):
        if not levels:
            return []
        sorted_lvls = sorted([l for _, l in levels])
        clusters = [[sorted_lvls[0]]]
        for lvl in sorted_lvls[1:]:
            if (lvl - clusters[-1][-1]) / clusters[-1][-1] * 100 < threshold_pct:
                clusters[-1].append(lvl)
            else:
                clusters.append([lvl])
        # Return cluster midpoint weighted by touch count
        result = []
        for cluster in clusters:
            avg = sum(cluster) / len(cluster)
            touches = len(cluster)
            result.append({"price": round(avg, 6), "touches": touches})
        return sorted(result, key=lambda x: x["touches"], reverse=True)

    resistance = cluster_levels(pivots_high)
    support = cluster_levels(pivots_low)

    # Volume-weighted support/resistance (high volume nodes)
    price_min, price_max = min(lows), max(highs)
    if price_max == price_min:
        return {"support": [], "resistance": [], "current": closes[-1]}

    bins = 20
    bin_size = (price_max - price_min) / bins
    vol_profile = {}
    for p, v in zip(closes, volumes):
        idx = min(int((p - price_min) / bin_size), bins - 1)
        vol_profile[idx] = vol_profile.get(idx, 0) + v

    avg_vol = sum(vol_profile.values()) / len(vol_profile) if vol_profile else 0
    hvn_levels = []
    for idx, vol in vol_profile.items():
        if vol > avg_vol * 1.2:
            price = price_min + (idx + 0.5) * bin_size
            hvn_levels.append({"price": round(price, 6), "volume_ratio": round(vol / avg_vol, 2)})

    current_price = closes[-1]
    return {
        "support": [s for s in support if s["price"] < current_price][:3],
        "resistance": [r for r in resistance if r["price"] > current_price][:3],
        "hvn": hvn_levels[:3],
        "current": current_price,
    }


# ── Volume Analysis ───────────────────────────────────────────────────────

def analyze_volume(klines: List[Dict]) -> Dict:
    """Analyze volume patterns and changes."""
    if len(klines) < 20:
        return {"signal": "INSUFFICIENT_DATA"}

    volumes = [k["volume"] for k in klines]
    closes = [k["close"] for k in klines]

    # Current vs average
    vol_5 = sum(volumes[-5:]) / 5
    vol_20 = sum(volumes[-20:]) / 20
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1.0

    # Volume trend (rising/falling)
    vol_first_half = sum(volumes[-20:-10]) / 10
    vol_second_half = sum(volumes[-10:]) / 10
    vol_trend = "rising" if vol_second_half > vol_first_half * 1.1 else \
                "falling" if vol_second_half < vol_first_half * 0.9 else "stable"

    # OBV analysis
    obv_vals = Indicators.obv(klines)
    if len(obv_vals) >= 20:
        obv_short = sum(obv_vals[-5:]) / 5
        obv_long = sum(obv_vals[-20:]) / 20
        obv_trend = "rising" if obv_short > obv_long else "falling"
    else:
        obv_trend = "unknown"

    # Price-volume divergence
    price_change = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] else 0
    vol_change = (vol_5 - vol_20) / vol_20 * 100 if vol_20 > 0 else 0

    divergence = "none"
    if price_change > 2 and vol_change < -20:
        divergence = "bearish_divergence — 價漲量縮 (看跌背離)"
    elif price_change < -2 and vol_change < -20:
        divergence = "capitulation — 恐慌性拋售接近尾聲"
    elif price_change > 2 and vol_change > 50:
        divergence = "bullish_breakout — 放量突破 (看漲)"

    # Signal
    signal = "neutral"
    if vol_ratio > 2.0:
        signal = "HIGH_VOLUME — 量能異常放大"
    elif vol_ratio > 1.5:
        signal = "ABOVE_AVERAGE — 量能偏高"
    elif vol_ratio < 0.5:
        signal = "LOW_VOLUME — 量能萎縮"
    else:
        signal = "NORMAL"

    return {
        "vol_5_avg": round(vol_5, 2),
        "vol_20_avg": round(vol_20, 2),
        "vol_ratio_5_20": round(vol_ratio, 2),
        "vol_trend": vol_trend,
        "obv_trend": obv_trend,
        "price_5d_change_pct": round(price_change, 2),
        "vol_5d_change_pct": round(vol_change, 2),
        "divergence": divergence,
        "signal": signal,
    }


# ── Volatility Analysis ───────────────────────────────────────────────────

def analyze_volatility(klines: List[Dict], daily_klines: List[Dict]) -> Dict:
    """Comprehensive volatility analysis."""
    if len(klines) < 14:
        return {}

    # ATR-based volatility
    atr_14 = Indicators.atr(klines, period=14)
    current_price = klines[-1]["close"]
    atr_pct = (atr_14 / current_price * 100) if current_price else 0

    # Historical volatility (annualized)
    closes = [k["close"] for k in klines]
    if len(closes) > 1:
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        daily_vol = (sum((r - sum(returns)/len(returns))**2 for r in returns) / len(returns)) ** 0.5
        annual_vol = daily_vol * math.sqrt(365) * 100
    else:
        daily_vol = 0
        annual_vol = 0

    # Bollinger Band width
    prices = [k["close"] for k in klines]
    bb = Indicators.bollinger_bands(prices, period=20)
    bb_width = ((bb["upper"] - bb["lower"]) / bb["middle"] * 100) if bb["middle"] else 0

    # BB squeeze detection
    squeeze_info = Indicators.bb_squeeze(klines)

    # Daily volatility from daily klines
    daily_ranges = []
    for k in daily_klines[-14:]:
        daily_ranges.append((k["high"] - k["low"]) / k["close"] * 100 if k["close"] else 0)
    avg_daily_range = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 0

    # Volatility regime
    if atr_pct > 5:
        regime = "EXTREME — 極端波動"
    elif atr_pct > 3:
        regime = "HIGH — 高波動"
    elif atr_pct > 1.5:
        regime = "MODERATE — 中等波動"
    elif atr_pct > 0.8:
        regime = "LOW — 低波動"
    else:
        regime = "QUIET — 極低波動"

    return {
        "atr_14": round(atr_14, 6),
        "atr_pct": round(atr_pct, 2),
        "annualized_vol_pct": round(annual_vol, 1),
        "bb_width_pct": round(bb_width, 2),
        "bb_squeeze": squeeze_info.get("squeezing", False),
        "bb_squeeze_pctile": round(squeeze_info.get("percentile", 0), 1),
        "avg_daily_range_pct": round(avg_daily_range, 2),
        "volatility_regime": regime,
    }


# ── Funding Rate Analysis ─────────────────────────────────────────────────

def fetch_funding_rate(symbol: str) -> Dict:
    """Fetch funding rate from Binance Futures API."""
    import requests
    coin = symbol.replace("USDT", "")
    result = {
        "funding_rate": None,
        "funding_rate_8h_avg": None,
        "signal": "N/A",
    }

    try:
        resp = requests.get(
            f"https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": f"{coin}USDT", "limit": 10},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data:
                rates = [float(d.get("fundingRate", 0)) for d in data]
                latest = rates[-1] * 100  # Convert to percentage
                avg_8h = sum(rates[-3:]) / len(rates[-3:]) * 100 if len(rates) >= 3 else latest

                result["funding_rate"] = round(latest, 4)
                result["funding_rate_8h_avg"] = round(avg_8h, 4)

                if latest > 0.05:
                    result["signal"] = "LONG_CROWDED — 多頭擁擠 (看跌信號)"
                elif latest > 0.01:
                    result["signal"] = "SLIGHT_LONG — 略偏多"
                elif latest < -0.05:
                    result["signal"] = "SHORT_CROWDED — 空頭擁擠 (看漲/軋空潛力)"
                elif latest < -0.01:
                    result["signal"] = "SLIGHT_SHORT — 略偏空"
                else:
                    result["signal"] = "NEUTRAL — 中性"
    except Exception as e:
        result["signal"] = f"ERROR: {e}"

    # Top trader long/short ratio
    try:
        resp = requests.get(
            "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
            params={"symbol": f"{coin}USDT", "period": "1h", "limit": 1},
            timeout=5,
        )
        if resp.status_code == 200 and resp.json():
            top = resp.json()[0]
            result["top_long_pct"] = round(float(top.get("longAccount", 0.5)) * 100, 1)
            result["top_short_pct"] = round(float(top.get("shortAccount", 0.5)) * 100, 1)
    except Exception:
        pass

    # Taker buy/sell ratio
    try:
        resp = requests.get(
            "https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol": f"{coin}USDT", "period": "1h", "limit": 1},
            timeout=5,
        )
        if resp.status_code == 200 and resp.json():
            taker = resp.json()[0]
            result["taker_buy_sell_ratio"] = round(float(taker.get("buySellRatio", 1.0)), 3)
    except Exception:
        pass

    return result


# ── 24h Stats ─────────────────────────────────────────────────────────────

def fetch_24h_stats(client: BinanceClient, symbol: str) -> Dict:
    """Fetch 24h trading statistics."""
    try:
        stats = client.get_24hr_stats(symbol)
        if stats:
            return {
                "price": float(stats.get("last_price", 0)),
                "price_change_pct": float(stats.get("price_change_percent", 0)),
                "high_24h": float(stats.get("high_price", 0)),
                "low_24h": float(stats.get("low_price", 0)),
                "volume_base": float(stats.get("volume", 0)),
                "volume_quote": float(stats.get("quote_volume", 0)),
                "trades_count": int(stats.get("count", 0)),
                "bid_price": float(stats.get("bid_price", 0)),
                "ask_price": float(stats.get("ask_price", 0)),
            }
    except Exception as e:
        return {"error": str(e)}
    return {}


# ── Main Analysis ─────────────────────────────────────────────────────────

def analyze_coin(client: BinanceClient, symbol: str) -> Dict:
    """Full technical analysis for a single coin."""
    coin = symbol.replace("USDT", "")
    print(f"  Analyzing {coin}...")

    result = {"symbol": symbol, "coin": coin}

    # 1. Fetch 24h stats
    rate_limited_sleep()
    result["stats_24h"] = fetch_24h_stats(client, symbol)

    # 2. Fetch klines for multiple timeframes
    kline_data = {}
    for tf_name, tf_config in TIMEFRAMES.items():
        rate_limited_sleep()
        try:
            klines = client.get_klines(symbol, tf_config["interval"], limit=tf_config["limit"])
            kline_data[tf_name] = klines
        except Exception as e:
            print(f"    Warning: Failed to fetch {tf_name} klines: {e}")
            kline_data[tf_name] = []

    result["klines"] = kline_data

    # 3. RSI/MACD analysis on daily and 4h
    for tf in ["1d", "4h", "1h"]:
        klines = kline_data.get(tf, [])
        if len(klines) >= 50:
            analysis = Indicators.analyze_symbol(klines)
            result[f"analysis_{tf}"] = analysis

    # 4. K-line patterns (from 4h and 1h)
    patterns = []
    for tf in ["4h", "1h"]:
        klines = kline_data.get(tf, [])
        if len(klines) >= 10:
            tf_patterns = detect_patterns(klines)
            for p in tf_patterns:
                patterns.append(f"[{tf}] {p}")
    result["patterns"] = patterns

    # 5. Support/Resistance (from daily)
    daily_klines = kline_data.get("1d", [])
    if daily_klines:
        result["sr_levels"] = find_support_resistance(daily_klines, lookback=60)
    else:
        result["sr_levels"] = {}

    # 6. Volume analysis (from daily)
    if daily_klines:
        result["volume"] = analyze_volume(daily_klines)
    else:
        result["volume"] = {}

    # 7. Volatility analysis
    h4_klines = kline_data.get("4h", [])
    if h4_klines:
        result["volatility"] = analyze_volatility(h4_klines, daily_klines if daily_klines else h4_klines)
    else:
        result["volatility"] = {}

    # 8. Funding rate
    rate_limited_sleep()
    result["funding"] = fetch_funding_rate(symbol)

    # 9. Overall signal synthesis
    result["signal_summary"] = synthesize_signal(result)

    return result


def synthesize_signal(analysis: Dict) -> Dict:
    """Synthesize all signals into a composite assessment."""
    bullish = 0
    bearish = 0
    reasons_bull = []
    reasons_bear = []

    # RSI signal
    rsi_1d = analysis.get("analysis_1d", {}).get("rsi", 50)
    if rsi_1d < 30:
        bullish += 2
        reasons_bull.append(f"RSI日線超賣({rsi_1d:.1f})")
    elif rsi_1d < 40:
        bullish += 1
        reasons_bull.append(f"RSI日線偏低({rsi_1d:.1f})")
    elif rsi_1d > 70:
        bearish += 2
        reasons_bear.append(f"RSI日線超買({rsi_1d:.1f})")
    elif rsi_1d > 60:
        bearish += 1
        reasons_bear.append(f"RSI日線偏高({rsi_1d:.1f})")

    # MACD signal
    macd_hist = analysis.get("analysis_1d", {}).get("macd_histogram", 0)
    if macd_hist > 0:
        bullish += 1
        reasons_bull.append("MACD日線柱狀正值")
    elif macd_hist < 0:
        bearish += 1
        reasons_bear.append("MACD日線柱狀負值")

    # Trend
    trend = analysis.get("analysis_1d", {}).get("trend", "sideways")
    if trend in ("strong_up", "weak_up"):
        bullish += 1
        reasons_bull.append(f"日線趨勢: {trend}")
    elif trend in ("strong_down", "weak_down"):
        bearish += 1
        reasons_bear.append(f"日線趨勢: {trend}")

    # BB position
    bb_upper = analysis.get("analysis_1d", {}).get("bb_upper", 0)
    bb_lower = analysis.get("analysis_1d", {}).get("bb_lower", 0)
    current = analysis.get("stats_24h", {}).get("price", 0)
    if current and bb_upper and bb_lower:
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            bb_position = (current - bb_lower) / bb_range
            if bb_position < 0.2:
                bullish += 1
                reasons_bull.append(f"接近布林下軌({bb_position:.0%})")
            elif bb_position > 0.8:
                bearish += 1
                reasons_bear.append(f"接近布林上軌({bb_position:.0%})")

    # Volume
    vol = analysis.get("volume", {})
    if vol.get("vol_ratio_5_20", 1) > 1.5:
        reasons_bull.append("量能放大") if "量能放大" not in " ".join(reasons_bull) else None
    if "bullish_breakout" in vol.get("divergence", ""):
        bullish += 2
        reasons_bull.append("放量突破")
    elif "bearish_divergence" in vol.get("divergence", ""):
        bearish += 2
        reasons_bear.append("價漲量縮")

    # Funding rate
    funding = analysis.get("funding", {})
    if funding.get("funding_rate") is not None:
        fr = funding["funding_rate"]
        if fr < -0.03:
            bullish += 1
            reasons_bull.append(f"資金費率負({fr:.4f}%) — 軋空潛力")
        elif fr > 0.05:
            bearish += 1
            reasons_bear.append(f"資金費率高({fr:.4f}%) — 多頭擁擠")

    # Patterns
    patterns = analysis.get("patterns", [])
    for p in patterns:
        if "看漲" in p or "Bullish" in p or "Hammer" in p:
            bullish += 1
            reasons_bull.append(f"K線形態: {p.split(']')[-1].strip()}")
        elif "看跌" in p or "Bearish" in p or "Shooting" in p:
            bearish += 1
            reasons_bear.append(f"K線形態: {p.split(']')[-1].strip()}")

    total = bullish + bearish
    if total == 0:
        verdict = "NEUTRAL — 中性"
    elif bullish > bearish * 2:
        verdict = "STRONG_BULL — 強烈看漲"
    elif bullish > bearish:
        verdict = "BULLISH — 偏多"
    elif bearish > bullish * 2:
        verdict = "STRONG_BEAR — 強烈看跌"
    elif bearish > bullish:
        verdict = "BEARISH — 偏空"
    else:
        verdict = "MIXED — 多空分歧"

    return {
        "verdict": verdict,
        "bull_score": bullish,
        "bear_score": bearish,
        "bull_reasons": reasons_bull,
        "bear_reasons": reasons_bear,
    }


# ── Report Generation ─────────────────────────────────────────────────────

def generate_report(analyses: List[Dict]) -> str:
    """Generate markdown technical analysis report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"# 技術面分析報告 (Technical Analysis Report)")
    lines.append(f"**生成時間:** {now}")
    lines.append(f"**分析幣種:** {len(analyses)} 個")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Summary Table ──
    lines.append("## 一、綜合評分總覽")
    lines.append("")
    lines.append("| 幣種 | 信號 | 多頭分 | 空頭分 | RSI(日) | MACD柱 | 趨勢 | 資金費率 |")
    lines.append("|------|------|--------|--------|---------|--------|------|----------|")
    for a in analyses:
        coin = a["coin"]
        sig = a.get("signal_summary", {})
        verdict = sig.get("verdict", "N/A").split("—")[0].strip()
        bull = sig.get("bull_score", 0)
        bear = sig.get("bear_score", 0)
        rsi = a.get("analysis_1d", {}).get("rsi", 50)
        macd_h = a.get("analysis_1d", {}).get("macd_histogram", 0)
        trend = a.get("analysis_1d", {}).get("trend", "N/A")
        fr = a.get("funding", {}).get("funding_rate")
        fr_str = f"{fr:.4f}%" if fr is not None else "N/A"
        macd_str = f"{macd_h:+.4f}" if macd_h else "0"
        lines.append(f"| {coin} | {verdict} | {bull} | {bear} | {rsi:.1f} | {macd_str} | {trend} | {fr_str} |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Detailed per-coin analysis ──
    for i, a in enumerate(analyses):
        coin = a["coin"]
        symbol = a["symbol"]
        stats = a.get("stats_24h", {})
        sig = a.get("signal_summary", {})

        lines.append(f"## {i+2}. {coin} — {sig.get('verdict', 'N/A')}")
        lines.append("")

        # Price info
        price = stats.get("price", 0)
        chg = stats.get("price_change_pct", 0)
        high = stats.get("high_24h", 0)
        low = stats.get("low_24h", 0)
        vol_q = stats.get("volume_quote", 0)
        lines.append(f"### 價格概覽")
        lines.append(f"- 現價: ${price:,.4f} ({chg:+.2f}% 24h)")
        lines.append(f"- 24h 區間: ${low:,.4f} — ${high:,.4f}")
        lines.append(f"- 24h 成交量: ${vol_q:,.0f}")
        lines.append("")

        # RSI / MACD
        for tf in ["1d", "4h", "1h"]:
            analysis = a.get(f"analysis_{tf}", {})
            if not analysis:
                continue
            tf_label = {"1d": "日線", "4h": "4小時", "1h": "1小時"}.get(tf, tf)
            rsi = analysis.get("rsi", 50)
            macd_val = analysis.get("macd", 0)
            macd_sig = analysis.get("macd_signal", 0)
            macd_h = analysis.get("macd_histogram", 0)
            trend = analysis.get("trend", "N/A")
            strength = analysis.get("trend_strength", 0)

            rsi_label = "超買" if rsi > 70 else "超賣" if rsi < 30 else "中性"
            macd_label = "金叉" if macd_val > macd_sig else "死叉"

            lines.append(f"### RSI / MACD ({tf_label})")
            lines.append(f"- RSI(14): {rsi:.1f} ({rsi_label})")
            lines.append(f"- MACD: {macd_val:.6f} | Signal: {macd_sig:.6f} | Histogram: {macd_h:+.6f}")
            lines.append(f"- MACD狀態: {macd_label}")
            lines.append(f"- 趨勢: {trend} (強度: {strength:.1f})")
            lines.append("")

        # K-line patterns
        patterns = a.get("patterns", [])
        lines.append("### K線形態")
        if patterns:
            for p in patterns:
                lines.append(f"- {p}")
        else:
            lines.append("- 無明顯形態信號")
        lines.append("")

        # Support / Resistance
        sr = a.get("sr_levels", {})
        lines.append("### 支撐阻力位")
        if sr.get("support"):
            lines.append("**支撐:**")
            for s in sr["support"][:3]:
                dist = (price - s["price"]) / price * 100 if price else 0
                lines.append(f"  - ${s['price']:,.4f} (觸碰 {s['touches']} 次, 距現價 {dist:+.2f}%)")
        if sr.get("resistance"):
            lines.append("**阻力:**")
            for r in sr["resistance"][:3]:
                dist = (r["price"] - price) / price * 100 if price else 0
                lines.append(f"  - ${r['price']:,.4f} (觸碰 {r['touches']} 次, 距現價 {dist:+.2f}%)")
        if sr.get("hvn"):
            lines.append("**高量節點 (HVN):**")
            for h in sr["hvn"][:3]:
                lines.append(f"  - ${h['price']:,.4f} (量比: {h['volume_ratio']}x)")
        lines.append("")

        # Volume
        vol = a.get("volume", {})
        if vol and vol.get("signal") != "INSUFFICIENT_DATA":
            lines.append("### 量能分析")
            lines.append(f"- 5日均量/20日均量比: {vol.get('vol_ratio_5_20', 'N/A')}x")
            lines.append(f"- 量能趨勢: {vol.get('vol_trend', 'N/A')}")
            lines.append(f"- OBV趨勢: {vol.get('obv_trend', 'N/A')}")
            lines.append(f"- 5日價格變化: {vol.get('price_5d_change_pct', 0):+.2f}%")
            lines.append(f"- 5日量能變化: {vol.get('vol_5d_change_pct', 0):+.2f}%")
            lines.append(f"- 量價背離: {vol.get('divergence', 'none')}")
            lines.append(f"- 量能信號: {vol.get('signal', 'N/A')}")
            lines.append("")

        # Volatility
        v = a.get("volatility", {})
        if v:
            lines.append("### 波動率分析")
            lines.append(f"- ATR(14): {v.get('atr_14', 0):.6f} ({v.get('atr_pct', 0):.2f}%)")
            lines.append(f"- 年化波動率: {v.get('annualized_vol_pct', 0):.1f}%")
            lines.append(f"- 布林帶寬度: {v.get('bb_width_pct', 0):.2f}%")
            squeeze = "是 ⚡" if v.get("bb_squeeze") else "否"
            lines.append(f"- 布林帶壓縮: {squeeze} (百分位: {v.get('bb_squeeze_pctile', 0):.0f}%)")
            lines.append(f"- 日均波幅: {v.get('avg_daily_range_pct', 0):.2f}%")
            lines.append(f"- 波動率區間: {v.get('volatility_regime', 'N/A')}")
            lines.append("")

        # Funding rate
        fr = a.get("funding", {})
        if fr.get("funding_rate") is not None:
            lines.append("### 資金費率")
            lines.append(f"- 當前資金費率: {fr.get('funding_rate', 0):.4f}%")
            lines.append(f"- 近8h均值: {fr.get('funding_rate_8h_avg', 0):.4f}%")
            lines.append(f"- 信號: {fr.get('signal', 'N/A')}")
            if fr.get("top_long_pct"):
                lines.append(f"- 大戶多空比: 多 {fr['top_long_pct']:.1f}% / 空 {fr.get('top_short_pct', 50):.1f}%")
            if fr.get("taker_buy_sell_ratio"):
                lines.append(f"- 主動買賣比: {fr['taker_buy_sell_ratio']:.3f}")
            lines.append("")

        # Signal summary
        lines.append("### 綜合判斷")
        lines.append(f"- **結論:** {sig.get('verdict', 'N/A')}")
        if sig.get("bull_reasons"):
            lines.append(f"- **看漲因素:** {'; '.join(sig['bull_reasons'])}")
        if sig.get("bear_reasons"):
            lines.append(f"- **看跌因素:** {'; '.join(sig['bear_reasons'])}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── Market Context ──
    lines.append("## 附錄：市場環境")
    lines.append("")
    lines.append("| 指標 | 值 |")
    lines.append("|------|-----|")

    # BTC context
    rate_limited_sleep()
    btc_stats = fetch_24h_stats(analyses[0]["_client"] if "_client" in analyses else None, "BTCUSDT")
    if btc_stats and "price" in btc_stats:
        lines.append(f"| BTC 現價 | ${btc_stats['price']:,.2f} ({btc_stats.get('price_change_pct', 0):+.2f}%) |")
        lines.append(f"| BTC 24h Vol | ${btc_stats.get('volume_quote', 0):,.0f} |")

    # BTC funding
    btc_funding = fetch_funding_rate("BTCUSDT")
    if btc_funding.get("funding_rate") is not None:
        lines.append(f"| BTC 資金費率 | {btc_funding['funding_rate']:.4f}% |")

    lines.append("")
    lines.append("---")
    lines.append(f"*報告由 technical_analysis.py 自動生成*")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Technical Analysis Report Generator")
    print("=" * 60)

    # Initialize client
    client = BinanceClient(testnet=False)
    print(f"BinanceClient initialized (testnet=False)")

    # Determine coins to analyze
    all_symbols = list(HOLDINGS)

    # Add scan candidates (check which are actually tradeable)
    print(f"\nChecking scan candidates...")
    for sym in SCAN_CANDIDATES:
        if sym not in all_symbols:
            try:
                rate_limited_sleep()
                stats = client.get_24hr_stats(sym)
                if stats and float(stats.get("quote_volume", 0)) > 1_000_000:
                    all_symbols.append(sym)
                    print(f"  + {sym} (vol ${float(stats.get('quote_volume',0))/1e6:.1f}M)")
            except Exception:
                pass

    print(f"\nAnalyzing {len(all_symbols)} coins: {', '.join(s.replace('USDT','') for s in all_symbols)}")
    print()

    # Run analysis
    analyses = []
    for sym in all_symbols:
        try:
            analysis = analyze_coin(client, sym)
            analysis["_client"] = client  # For BTC context in report
            analyses.append(analysis)
            print(f"  ✓ {sym} complete")
        except Exception as e:
            print(f"  ✗ {sym} failed: {e}")

    # Generate report
    print(f"\nGenerating report...")
    report = generate_report(analyses)

    # Save report
    date_str = datetime.now().strftime("%Y-%m-%d")
    output_path = PROJECT_ROOT / "data" / f"technical_analysis_{date_str}.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nReport saved to: {output_path}")
    print(f"Total coins analyzed: {len(analyses)}")
    print("=" * 60)

    # Print summary to stdout
    print("\n📊 Summary:")
    for a in analyses:
        sig = a.get("signal_summary", {})
        coin = a["coin"]
        verdict = sig.get("verdict", "N/A")
        bull = sig.get("bull_score", 0)
        bear = sig.get("bear_score", 0)
        print(f"  {coin:10s} | {verdict:30s} | Bull:{bull} Bear:{bear}")

    return analyses


if __name__ == "__main__":
    main()
