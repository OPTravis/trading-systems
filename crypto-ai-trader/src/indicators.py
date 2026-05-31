"""
Technical Indicators Calculator
"""

# Python 3.11.15 (uv build) removed random.randbits; numpy expects it
import random as _random

if not hasattr(_random, "randbits"):
    _random.randbits = _random.getrandbits  # type: ignore[attr-defined]

from typing import Any, Dict, List

import pandas as pd


class Indicators:
    """Technical indicators for market analysis"""

    @staticmethod
    def sma(prices: List[float], period: int) -> float:
        """Simple Moving Average"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        return sum(prices[-period:]) / period

    @staticmethod
    def ema(prices: List[float], period: int) -> float:
        """Exponential Moving Average"""
        if len(prices) < period:
            return prices[-1] if prices else 0

        df = pd.Series(prices)
        return df.ewm(span=period, adjust=False).mean().iloc[-1]

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> float:
        """Relative Strength Index using Wilder's smoothing"""
        if len(prices) < period + 1:
            return 50.0

        df = pd.Series(prices)
        delta = df.diff().dropna()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        # Seed with simple average of first 'period' values
        avg_gain = gain.iloc[:period].mean()
        avg_loss = loss.iloc[:period].mean()

        # Wilder's smoothing for the rest
        for i in range(period, len(gain)):
            avg_gain = (avg_gain * (period - 1) + gain.iloc[i]) / period
            avg_loss = (avg_loss * (period - 1) + loss.iloc[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def macd(
        prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Dict[str, float]:
        """MACD (Moving Average Convergence Divergence)"""
        if len(prices) < slow + signal:
            return {"macd": 0, "signal": 0, "histogram": 0}

        df = pd.Series(prices)
        ema_fast = df.ewm(span=fast, adjust=False).mean()
        ema_slow = df.ewm(span=slow, adjust=False).mean()

        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return {
            "macd": macd_line.iloc[-1],
            "signal": signal_line.iloc[-1],
            "histogram": histogram.iloc[-1],
        }

    @staticmethod
    def bollinger_bands(
        prices: List[float], period: int = 20, std_dev: float = 2.0
    ) -> Dict[str, float]:
        """Bollinger Bands"""
        if len(prices) < period:
            return {"upper": prices[-1], "middle": prices[-1], "lower": prices[-1]}

        df = pd.Series(prices)
        middle = df.rolling(window=period).mean()
        std = df.rolling(window=period).std()

        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)

        return {
            "upper": upper.iloc[-1],
            "middle": middle.iloc[-1],
            "lower": lower.iloc[-1],
        }

    @staticmethod
    def vwap(klines: List[Dict]) -> float:
        """Volume Weighted Average Price"""
        if not klines:
            return 0

        total_pv = sum(
            ((k["high"] + k["low"] + k["close"]) / 3) * k["volume"] for k in klines
        )
        total_volume = sum(k["volume"] for k in klines)

        return total_pv / total_volume if total_volume > 0 else 0

    @staticmethod
    def volume_profile(klines: List[Dict], bins: int = 20) -> Dict:
        """Volume Profile - find high volume nodes"""
        if not klines or len(klines) < bins:
            return {"vpn": [], "prices": []}

        prices = [(k["high"] + k["low"] + k["close"]) / 3 for k in klines]
        volumes = [k["volume"] for k in klines]

        # Create price bins
        price_min, price_max = min(prices), max(prices)
        if price_max == price_min:
            return {"vpn": [price_min], "bin_size": 0}
        bin_size = (price_max - price_min) / bins

        profile: Dict[int, float] = {}
        for p, v in zip(prices, volumes):
            bin_idx = int((p - price_min) / bin_size)
            bin_idx = min(bin_idx, bins - 1)
            profile[bin_idx] = profile.get(bin_idx, 0) + v

        # Find high volume nodes
        avg_volume = sum(profile.values()) / len(profile) if profile else 0
        vpn = [price_min + (i * bin_size) for i, v in profile.items() if v > avg_volume]

        return {"vpn": vpn, "bin_size": bin_size}

    @staticmethod
    def atr(klines: List[Dict], period: int = 14) -> float:
        """Average True Range (volatility)"""
        if len(klines) < period + 1:
            return 0

        trs = []
        for i in range(1, len(klines)):
            high = klines[i]["high"]
            low = klines[i]["low"]
            prev_close = klines[i - 1]["close"]

            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        return sum(trs[-period:]) / period if trs else 0

    @staticmethod
    def adx(klines: List[Dict], period: int = 14) -> float:
        """Average Directional Index (trend strength)"""
        if len(klines) < period + 1:
            return 0

        # Calculate +DM and -DM
        plus_dm = []
        minus_dm = []

        for i in range(1, len(klines)):
            high = klines[i]["high"]
            low = klines[i]["low"]
            prev_high = klines[i - 1]["high"]
            prev_low = klines[i - 1]["low"]

            up_move = high - prev_high
            down_move = prev_low - low

            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        # Calculate ATR
        atr = Indicators.atr(klines, period)

        if atr == 0:
            return 0

        # Wilder's smoothing (same EMA method as RSI)
        # Initialize with simple average of first 'period' values
        smoothed_plus_dm = sum(plus_dm[:period]) / period
        smoothed_minus_dm = sum(minus_dm[:period]) / period
        smoothed_tr = (
            sum(
                [
                    Indicators._true_range(klines[i], klines[i - 1])
                    for i in range(1, period + 1)
                ]
            )
            / period
        )

        # Smooth remaining values
        for i in range(period, len(plus_dm)):
            smoothed_plus_dm = (smoothed_plus_dm * (period - 1) + plus_dm[i]) / period
            smoothed_minus_dm = (
                smoothed_minus_dm * (period - 1) + minus_dm[i]
            ) / period
            smoothed_tr = (
                smoothed_tr * (period - 1)
                + Indicators._true_range(klines[i + 1], klines[i])
            ) / period

        if smoothed_tr == 0:
            return 0

        plus_di = (smoothed_plus_dm / smoothed_tr) * 100
        minus_di = (smoothed_minus_dm / smoothed_tr) * 100

        dx = (
            (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
            if (plus_di + minus_di) > 0
            else 0
        )

        return dx

    @staticmethod
    def _true_range(current, prev):
        """Calculate True Range for one bar"""
        return max(
            current["high"] - current["low"],
            abs(current["high"] - prev["close"]),
            abs(current["low"] - prev["close"]),
        )

    @staticmethod
    def trend_strength(klines: List[Dict]) -> Dict[str, Any]:
        """Calculate overall trend strength"""
        if len(klines) < 50:
            return {"trend": "unknown", "strength": 0}

        prices = [k["close"] for k in klines]

        # Multiple MA comparison
        ma7 = Indicators.sma(prices, 7)
        ma25 = Indicators.sma(prices, 25)
        ma99 = Indicators.sma(prices, 99)

        current = prices[-1]

        # Determine trend
        if ma7 > ma25 > ma99 and current > ma7:
            trend = "strong_up"
            strength = min((current - ma99) / ma99 * 100, 20)
        elif ma7 < ma25 < ma99 and current < ma7:
            trend = "strong_down"
            strength = min((ma99 - current) / ma99 * 100, 20)
        elif ma7 > ma25:
            trend = "weak_up"
            strength = 5
        elif ma7 < ma25:
            trend = "weak_down"
            strength = 5
        else:
            trend = "sideways"
            strength = 2

        return {"trend": trend, "strength": strength}

    @staticmethod
    def momentum(klines: List[Dict], period: int = 10) -> float:
        """Price momentum (% change over period)"""
        if len(klines) < period:
            return 0

        current = klines[-1]["close"]
        previous = klines[-period]["close"]

        return ((current - previous) / previous) * 100

    @staticmethod
    def analyze_symbol(klines: List[Dict]) -> Dict:
        """Complete technical analysis for a symbol"""
        if not klines or len(klines) < 50:
            return {}

        prices = [k["close"] for k in klines]

        # Core indicators
        rsi = Indicators.rsi(prices)
        macd = Indicators.macd(prices)
        bb = Indicators.bollinger_bands(prices)
        trend = Indicators.trend_strength(klines)
        mom = Indicators.momentum(klines)
        atr = Indicators.atr(klines)
        vwap_val = Indicators.vwap(klines[-24:])  # 24h VWAP

        # Moving averages
        ma7 = Indicators.sma(prices, 7)
        ma25 = Indicators.sma(prices, 25)
        ma99 = Indicators.sma(prices, 99)

        current = prices[-1]
        volatility_pct = (atr / current) * 100

        return {
            "rsi": rsi,
            "macd": macd["macd"],
            "macd_signal": macd["signal"],
            "macd_histogram": macd["histogram"],
            "bb_upper": bb["upper"],
            "bb_middle": bb["middle"],
            "bb_lower": bb["lower"],
            "trend": trend["trend"],
            "trend_strength": trend["strength"],
            "momentum": mom,
            "atr": atr,
            "volatility_pct": volatility_pct,
            "vwap": vwap_val,
            "ma7": ma7,
            "ma25": ma25,
            "ma99": ma99,
            "current_price": current,
            "ma_position": (current - ma99) / ma99 * 100,
        }

    # =================================================================
    # NEW: Pre-pump detection indicators (Phase 3 upgrade)
    # =================================================================

    @staticmethod
    def obv(klines: List[Dict]) -> List[float]:
        """On-Balance Volume — cumulative volume flow.

        Rising OBV while price flat = accumulation.
        Returns list of OBV values (same length as klines).
        """
        if not klines:
            return []
        obv_values = [0.0]
        for i in range(1, len(klines)):
            if klines[i]["close"] > klines[i - 1]["close"]:
                obv_values.append(obv_values[-1] + klines[i]["volume"])
            elif klines[i]["close"] < klines[i - 1]["close"]:
                obv_values.append(obv_values[-1] - klines[i]["volume"])
            else:
                obv_values.append(obv_values[-1])
        return obv_values

    @staticmethod
    def obv_divergence(klines: List[Dict], lookback: int = 20) -> Dict:
        """Detect bullish OBV divergence: price makes lower low but OBV makes higher low.

        Returns: {detected: bool, strength: float 0-100, obv_trend: str}
        """
        if len(klines) < lookback + 5:
            return {"detected": False, "strength": 0, "obv_trend": "unknown"}

        obv_vals = Indicators.obv(klines)
        if not obv_vals:
            return {"detected": False, "strength": 0, "obv_trend": "unknown"}

        prices = [k["close"] for k in klines]
        half = lookback // 2

        # Compare two halves: first half vs second half
        first_half_prices = prices[-lookback:-half]
        second_half_prices = prices[-half:]
        first_half_obv = obv_vals[-lookback:-half]
        second_half_obv = obv_vals[-half:]

        price_lower_low = min(second_half_prices) < min(first_half_prices)
        obv_higher_low = min(second_half_obv) > min(first_half_obv)

        # OBV trend: is it rising?
        obv_sma_short = sum(obv_vals[-5:]) / 5
        obv_sma_long = sum(obv_vals[-20:]) / min(20, len(obv_vals))
        obv_trend = "rising" if obv_sma_short > obv_sma_long else "falling"

        detected = price_lower_low and obv_higher_low
        strength = 0.0
        if detected:
            price_drop = (
                (min(first_half_prices) - min(second_half_prices))
                / min(first_half_prices)
                * 100
            )
            obv_rise = (
                (min(second_half_obv) - min(first_half_obv))
                / abs(min(first_half_obv))
                * 100
                if min(first_half_obv) != 0
                else 0
            )
            strength = min(100, price_drop * 10 + obv_rise * 5)

        return {"detected": detected, "strength": strength, "obv_trend": obv_trend}

    @staticmethod
    def bb_squeeze(klines: List[Dict], period: int = 20, lookback: int = 50) -> Dict:
        """Detect Bollinger Band squeeze: bandwidth compressed to N-period low.

        Returns: {squeezing: bool, bandwidth_pct: float, percentile: float 0-100}
        """
        if len(klines) < lookback:
            return {"squeezing": False, "bandwidth_pct": 0, "percentile": 0}

        prices = [k["close"] for k in klines]

        # Current bandwidth
        bb = Indicators.bollinger_bands(prices, period)
        mid = bb["middle"]
        bw = (bb["upper"] - bb["lower"]) / mid * 100 if mid > 0 else 0

        # Historical bandwidths over lookback
        bw_history = []
        for i in range(period + 1, min(lookback + 1, len(prices) + 1)):
            sub = prices[:i]
            b = Indicators.bollinger_bands(sub, period)
            m = b["middle"]
            if m > 0:
                bw_history.append((b["upper"] - b["lower"]) / m * 100)

        if not bw_history:
            return {"squeezing": False, "bandwidth_pct": bw, "percentile": 0}

        below = sum(1 for x in bw_history if x < bw)
        percentile = below / len(bw_history) * 100
        squeezing = percentile <= 20

        return {"squeezing": squeezing, "bandwidth_pct": bw, "percentile": percentile}

    @staticmethod
    def rsi_divergence(
        klines: List[Dict], period: int = 14, lookback: int = 30
    ) -> Dict:
        """Detect bullish RSI divergence: price lower low but RSI higher low.

        Returns: {detected: bool, strength: float 0-100, rsi_current: float}
        """
        if len(klines) < lookback + period:
            return {"detected": False, "strength": 0, "rsi_current": 50}

        prices = [k["close"] for k in klines]
        rsi_vals = []
        for i in range(period + 1, len(prices) + 1):
            rsi_vals.append(Indicators.rsi(prices[:i], period))

        if len(rsi_vals) < lookback:
            return {
                "detected": False,
                "strength": 0,
                "rsi_current": rsi_vals[-1] if rsi_vals else 50,
            }

        half = lookback // 2
        first_half_prices = prices[-lookback:-half]
        second_half_prices = prices[-half:]
        first_half_rsi = rsi_vals[-lookback:-half]
        second_half_rsi = rsi_vals[-half:]

        price_lower_low = min(second_half_prices) < min(first_half_prices)
        rsi_higher_low = min(second_half_rsi) > min(first_half_rsi)

        detected = price_lower_low and rsi_higher_low
        strength = 0.0
        if detected:
            price_drop = (
                (min(first_half_prices) - min(second_half_prices))
                / min(first_half_prices)
                * 100
            )
            rsi_rise = min(second_half_rsi) - min(first_half_rsi)
            strength = min(100, price_drop * 8 + rsi_rise * 5)

        return {
            "detected": detected,
            "strength": strength,
            "rsi_current": rsi_vals[-1] if rsi_vals else 50,
        }

    @staticmethod
    def consolidation_breakout(
        klines: List[Dict], min_days: int = 30, max_range_pct: float = 25.0
    ) -> Dict:
        """Detect long-term consolidation breakout.

        Returns: {in_consolidation: bool, breaking_out: bool, range_pct: float,
                  days_in_range: int, volume_confirmed: bool}
        """
        if len(klines) < min_days + 5:
            return {
                "in_consolidation": False,
                "breaking_out": False,
                "range_pct": 0,
                "days_in_range": 0,
                "volume_confirmed": False,
            }

        prices = [k["close"] for k in klines]
        volumes = [k["volume"] for k in klines]

        range_prices = prices[-(min_days + 3) : -3]
        range_high = max(range_prices)
        range_low = min(range_prices)
        range_pct = (
            ((range_high - range_low) / range_low * 100) if range_low > 0 else 999
        )

        in_consolidation = range_pct <= max_range_pct

        current_price = prices[-1]
        breakout = current_price > range_high and in_consolidation

        recent_vol = sum(volumes[-3:]) / 3
        range_vol_avg = sum(volumes[-(min_days + 3) : -3]) / min_days
        volume_confirmed = (
            recent_vol > range_vol_avg * 1.5 if range_vol_avg > 0 else False
        )

        days_in_range = 0
        for p in reversed(prices):
            if range_low <= p <= range_high:
                days_in_range += 1
            else:
                break

        return {
            "in_consolidation": in_consolidation,
            "breaking_out": breakout,
            "range_pct": range_pct,
            "days_in_range": days_in_range,
            "volume_confirmed": volume_confirmed,
        }

    # -------------------------------------------------------------------------
    # BTC Multi-Factor Trend Score
    # -------------------------------------------------------------------------

    @staticmethod
    def btc_trend_score(klines: List[Dict]) -> Dict:
        """Multi-factor BTC trend scoring (0-100).

        Replaces the old SMA200-only check with a composite score:
          1. EMA Cross (EMA21 vs EMA55)       — 30%
          2. RSI Momentum (14-period)          — 20%
          3. MACD Histogram direction          — 20%
          4. Price structure (higher lows)     — 15%
          5. Volume trend (OBV slope)          — 15%

        Returns: {
            score: float (0-100),
            trend: "BULLISH" | "NEUTRAL" | "BEARISH",
            allow_long: bool,
            factors: {ema_cross, rsi, macd, price_structure, volume},
            ema_21, ema_55, rsi_14, macd_hist,
            sma_200, sma_50, btc_close,
        }
        """
        if len(klines) < 60:
            return {
                "score": 50,
                "trend": "NEUTRAL",
                "allow_long": True,
                "factors": {},
                "ema_21": 0,
                "ema_55": 0,
                "rsi_14": 50,
                "macd_hist": 0,
                "sma_200": 0,
                "sma_50": 0,
                "btc_close": 0,
            }

        closes = [k["close"] for k in klines]
        [k["volume"] for k in klines]

        # --- Factor 1: EMA Cross (30%) ---
        ema_21 = Indicators.ema(closes, 21)
        ema_55 = Indicators.ema(closes, 55)
        ema_diff_pct = (ema_21 - ema_55) / ema_55 * 100 if ema_55 > 0 else 0
        # Score: -3% → 0, 0% → 50, +3% → 100
        ema_score = max(0, min(100, 50 + (ema_diff_pct / 3) * 50))

        # --- Factor 2: RSI Momentum (20%) ---
        rsi_14 = Indicators.rsi(closes, 14)
        # RSI 30-70 range maps to 0-100; <30 oversold (contrarian bullish), >70 overbought
        if rsi_14 <= 30:
            rsi_score = 60.0  # oversold = potential bounce
        elif rsi_14 <= 45:
            rsi_score = 35.0 + (rsi_14 - 30) / 15 * 15  # 35-50
        elif rsi_14 <= 55:
            rsi_score = 50.0  # neutral
        elif rsi_14 <= 70:
            rsi_score = 50.0 + (rsi_14 - 55) / 15 * 30  # 50-80
        else:
            rsi_score = 70.0  # overbought = risk of pullback

        # --- Factor 3: MACD Histogram (20%) ---
        macd_data = Indicators.macd(closes)
        hist = macd_data["histogram"]
        # Normalize: hist magnitude relative to price
        hist_pct = hist / closes[-1] * 1000 if closes[-1] > 0 else 0
        # -2 → 0, 0 → 50, +2 → 100
        macd_score = max(0, min(100, 50 + (hist_pct / 2) * 50))

        # --- Factor 4: Price Structure — Higher Lows (15%) ---
        # Compare recent 3 swing lows (approximated by 10-day minima)
        if len(closes) >= 30:
            low_1 = min(closes[-10:])
            low_2 = min(closes[-20:-10])
            low_3 = min(closes[-30:-20])
            higher_lows = (low_1 > low_2) and (low_2 > low_3)
            one_higher = (low_1 > low_2) or (low_2 > low_3)
            if higher_lows:
                struct_score = 80
            elif one_higher:
                struct_score = 55
            elif low_1 < low_2 and low_2 < low_3:  # lower lows
                struct_score = 20
            else:
                struct_score = 40
        else:
            struct_score = 50

        # --- Factor 5: Volume Trend — OBV Slope (15%) ---
        obv_vals = Indicators.obv(klines)
        if len(obv_vals) >= 10:
            obv_recent = obv_vals[-1]
            obv_10_ago = obv_vals[-10]
            if obv_10_ago != 0:
                obv_slope = (obv_recent - obv_10_ago) / abs(obv_10_ago) * 100
            else:
                obv_slope = 0
            # -20% → 0, 0% → 50, +20% → 100
            vol_score = max(0, min(100, 50 + (obv_slope / 20) * 50))
        else:
            vol_score = 50

        # --- Composite Score ---
        score = (
            ema_score * 0.30
            + rsi_score * 0.20
            + macd_score * 0.20
            + struct_score * 0.15
            + vol_score * 0.15
        )

        # --- Legacy compatibility fields ---
        sma_200 = Indicators.sma(closes, 200) if len(closes) >= 200 else closes[-1]
        sma_50 = Indicators.sma(closes, 50)

        # --- Trend classification ---
        if score >= 65:
            trend = "BULLISH"
            allow_long = True
        elif score <= 35:
            trend = "BEARISH"
            allow_long = False
        else:
            trend = "NEUTRAL"
            allow_long = True

        return {
            "score": round(score, 1),
            "trend": trend,
            "allow_long": allow_long,
            "factors": {
                "ema_cross": round(ema_score, 1),
                "rsi": round(rsi_score, 1),
                "macd": round(macd_score, 1),
                "price_structure": round(struct_score, 1),
                "volume": round(vol_score, 1),
            },
            "ema_21": round(ema_21, 2),
            "ema_55": round(ema_55, 2),
            "rsi_14": round(rsi_14, 1),
            "macd_hist": round(hist, 4),
            "sma_200": round(sma_200, 2),
            "sma_50": round(sma_50, 2),
            "btc_close": round(closes[-1], 2),
        }
