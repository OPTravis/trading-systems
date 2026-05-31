"""
Multi-Timeframe Analysis - Trend confirmation across 4h/1h/15m
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .exchange_client import ExchangeClient
from .indicators import Indicators

logger = logging.getLogger(__name__)


class _RateLimiter:
    """Simple rate limiter: track timestamps, sleep if too fast."""

    def __init__(self, max_per_second: float = 25):
        self._max_per_second = max_per_second
        self._timestamps: List[float] = []
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            # Keep only last 2 seconds of history
            self._timestamps = [t for t in self._timestamps if now - t < 2.0]
            if len(self._timestamps) >= int(self._max_per_second * 2):
                sleep_time = self._timestamps[0] + 2.0 - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.monotonic()
            self._timestamps.append(now)


class MultiTimeframeAnalyzer:
    """Orchestrate trend confirmation across 4h, 1h, and 15m timeframes."""

    def __init__(self, binance_client: "ExchangeClient"):
        self.client = binance_client
        self.indicators = Indicators()
        self._rate_limiter = _RateLimiter(max_per_second=25)

    def analyze(self, symbol: str) -> Dict:
        """Run multi-timeframe analysis for a single symbol.

        Returns a dict with trend alignment, per-timeframe analysis,
        entry signal, composite trend score, and 15m ATR.
        """
        try:
            analysis_4h, analysis_1h, analysis_15m, atr_15m = self._fetch_and_analyze(
                symbol
            )
        except Exception as e:
            logger.error(f"Multi-TF analysis failed for {symbol}: {e}")
            return self._empty_result(symbol)

        if not analysis_4h and not analysis_1h and not analysis_15m:
            return self._empty_result(symbol)

        trend_alignment = self._determine_trend_alignment(
            analysis_4h, analysis_1h, analysis_15m
        )
        entry_signal = self._determine_entry_signal(
            analysis_4h, analysis_1h, analysis_15m
        )
        trend_score = self._calculate_trend_score(
            analysis_4h, analysis_1h, analysis_15m
        )

        return {
            "symbol": symbol,
            "trend_alignment": trend_alignment,
            "tf_4h": analysis_4h or {},
            "tf_1h": analysis_1h or {},
            "tf_15m": analysis_15m or {},
            "entry_signal": entry_signal,
            "trend_score": trend_score,
            "atr_15m": atr_15m,
        }

    def analyze_batch(self, symbols: List[str]) -> List[Dict]:
        """Analyze multiple symbols concurrently (max 3 workers)."""
        max_workers = min(3, len(symbols))
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.analyze, s): s for s in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.warning(f"Batch analysis failed for {sym}: {e}")
                    results.append(self._empty_result(sym))
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_and_analyze(self, symbol: str):
        """Fetch klines for all three timeframes (rate-limited) and run analysis."""
        # 4h – 100 bars
        self._rate_limiter.wait()
        klines_4h = self.client.get_klines(symbol, "4h", limit=100)
        analysis_4h = (
            self.indicators.analyze_symbol(klines_4h) if len(klines_4h) >= 50 else {}
        )

        # 1h – 100 bars
        self._rate_limiter.wait()
        klines_1h = self.client.get_klines(symbol, "1h", limit=100)
        analysis_1h = (
            self.indicators.analyze_symbol(klines_1h) if len(klines_1h) >= 50 else {}
        )

        # 15m – 200 bars
        self._rate_limiter.wait()
        klines_15m = self.client.get_klines(symbol, "15m", limit=200)
        analysis_15m = (
            self.indicators.analyze_symbol(klines_15m) if len(klines_15m) >= 50 else {}
        )

        # 15m ATR for entry precision
        atr_15m = self.indicators.atr(klines_15m) if len(klines_15m) >= 15 else 0.0

        return analysis_4h, analysis_1h, analysis_15m, atr_15m

    @staticmethod
    def _determine_trend_alignment(a_4h: Dict, a_1h: Dict, a_15m: Dict) -> str:
        """Classify overall trend alignment as bullish / bearish / mixed / neutral.

        Symmetrical logic: bullish and bearish use mirrored criteria in opposite
        directions (RSI thresholds inverted, MACD sign flipped, etc.).
        """
        trend_4h = a_4h.get("trend", "sideways")
        rsi_1h = a_1h.get("rsi", 50)
        rsi_15m = a_15m.get("rsi", 50)
        macd_hist_15m = a_15m.get("macd_histogram", 0)

        is_4h_bullish = trend_4h in ("strong_up", "weak_up")
        is_4h_bearish = trend_4h in ("strong_down", "weak_down")

        # --- 15m micro-structure (symmetrical) ---
        # Bullish: RSI in pullback zone (30-50) or MACD positive + RSI above midline
        is_15m_bull_pullback = 30 <= rsi_15m <= 50
        is_15m_bull_breakout = macd_hist_15m > 0 and rsi_15m > 50
        # Bearish: RSI in overbought-pullback zone (50-70) or MACD negative + RSI below midline
        is_15m_bear_pullback = 50 <= rsi_15m <= 70
        is_15m_bear_breakout = macd_hist_15m < 0 and rsi_15m < 50

        # --- Signal counting (symmetrical) ---
        bullish_signals = 0
        bearish_signals = 0

        if is_4h_bullish:
            bullish_signals += 1
        if is_4h_bearish:
            bearish_signals += 1

        # 1h RSI: bullish when < 45 (room to run up), bearish when > 55 (room to run down)
        if rsi_1h < 45:
            bullish_signals += 1
        if rsi_1h > 55:
            bearish_signals += 1

        if is_15m_bull_pullback or is_15m_bull_breakout:
            bullish_signals += 1
        if is_15m_bear_pullback or is_15m_bear_breakout:
            bearish_signals += 1

        # --- Classification (symmetrical) ---
        # BULLISH: 4h bullish AND 1h RSI < 60 AND 15m bullish setup
        if (
            is_4h_bullish
            and rsi_1h < 60
            and (is_15m_bull_pullback or is_15m_bull_breakout)
        ):
            return "bullish"

        # BEARISH: 4h bearish AND 1h RSI > 40 AND 15m bearish setup
        if (
            is_4h_bearish
            and rsi_1h > 40
            and (is_15m_bear_pullback or is_15m_bear_breakout)
        ):
            return "bearish"

        # MIXED: conflicting signals between timeframes
        if bullish_signals > 0 and bearish_signals > 0:
            return "mixed"

        # NEUTRAL: no clear direction
        return "neutral"

    @staticmethod
    def _determine_entry_signal(a_4h: Dict, a_1h: Dict, a_15m: Dict) -> Optional[str]:
        """Entry signal logic gated by trend direction.

        Returns:
            "long"   – bullish entry confirmed
            "short"  – bearish entry confirmed
            None     – no valid entry

        LONG ENTRY:  4h bullish AND 1h MACD histogram > 0 AND (15m RSI 35-55 OR
                     (1h RSI < 65 AND 15m RSI < 60))
        SHORT ENTRY: 4h bearish AND 1h MACD histogram < 0 AND (15m RSI 45-65 OR
                     (1h RSI > 35 AND 15m RSI > 40))
        """
        trend_4h = a_4h.get("trend", "sideways")
        macd_hist_1h = a_1h.get("macd_histogram", 0)
        rsi_1h = a_1h.get("rsi", 50)
        rsi_15m = a_15m.get("rsi", 50)

        is_4h_bullish = trend_4h in ("strong_up", "weak_up")
        is_4h_bearish = trend_4h in ("strong_down", "weak_down")

        # --- LONG ENTRY (symmetrical with short) ---
        if is_4h_bullish and macd_hist_1h > 0:
            # STRONG: pullback zone on 15m
            if 35 <= rsi_15m <= 55:
                return "long"
            # MODERATE: momentum continuation, gate with 1h not overbought
            if rsi_1h < 65 and rsi_15m < 60:
                return "long"
        # TREND-FOLLOWING: already in trend, not overbought
        if is_4h_bullish and rsi_1h < 70:
            return "long"

        # --- SHORT ENTRY (symmetrical mirror of long) ---
        if is_4h_bearish and macd_hist_1h < 0:
            # STRONG: overbought pullback zone on 15m
            if 45 <= rsi_15m <= 65:
                return "short"
            # MODERATE: momentum continuation, gate with 1h not oversold
            if rsi_1h > 35 and rsi_15m > 40:
                return "short"
        # TREND-FOLLOWING: already in downtrend, not oversold
        if is_4h_bearish and rsi_1h > 30:
            return "short"

        return None

    @staticmethod
    def _calculate_trend_score(a_4h: Dict, a_1h: Dict, a_15m: Dict) -> float:
        """Compute a composite trend score from 0 to 100.

        Scoring:
          +25  4h MA alignment (MA7 > MA25 > MA99)
          +15  4h MACD positive
          +10  1h MACD positive
          +10  15m MACD positive
          +10  4h RSI 40-60 (healthy zone)
          +15  1h RSI 35-55 (pullback zone)
          +15  15m volume surge
          -20  4h bearish MA alignment (MA7 < MA25 < MA99)
          -15  1h RSI > 70
        """
        score = 0.0

        # --- 4h MA alignment ---
        ma7_4h = a_4h.get("ma7", 0)
        ma25_4h = a_4h.get("ma25", 0)
        ma99_4h = a_4h.get("ma99", 0)

        if ma7_4h and ma25_4h and ma99_4h:
            if ma7_4h > ma25_4h > ma99_4h:
                score += 25
            elif ma7_4h < ma25_4h < ma99_4h:
                score -= 20

        # --- 4h MACD positive ---
        if a_4h.get("macd_histogram", 0) > 0:
            score += 15

        # --- 1h MACD positive ---
        if a_1h.get("macd_histogram", 0) > 0:
            score += 10

        # --- 15m MACD positive ---
        if a_15m.get("macd_histogram", 0) > 0:
            score += 10

        # --- 4h RSI healthy (40-60) ---
        rsi_4h = a_4h.get("rsi", 50)
        if 40 <= rsi_4h <= 60:
            score += 10

        # --- 1h RSI pullback zone (35-55) ---
        rsi_1h = a_1h.get("rsi", 50)
        if 35 <= rsi_1h <= 55:
            score += 15

        # --- 15m volume surge ---
        # Use the 15m current_price / bb / volume data for surge detection.
        # Since analyze_symbol doesn't directly return a volume_surge flag,
        # we detect it from relative volume context: if 15m has high momentum
        # AND MACD positive we treat that as a volume proxy.
        # A proper volume surge would need raw klines; we use momentum as a
        # proxy signal here.
        momentum_15m = a_15m.get("momentum", 0)
        if momentum_15m > 0.5:  # positive short-term momentum
            score += 15

        # --- Penalties ---
        # 1h RSI > 70 (overbought) – hurts longs
        if rsi_1h > 70:
            score -= 15
        # 1h RSI < 30 (oversold) – hurts shorts
        if rsi_1h < 30:
            score += 15

        return max(0.0, min(100.0, score))

    @staticmethod
    def _empty_result(symbol: str) -> Dict:
        """Return a neutral empty result when analysis cannot be performed."""
        return {
            "symbol": symbol,
            "trend_alignment": "neutral",
            "tf_4h": {},
            "tf_1h": {},
            "tf_15m": {},
            "entry_signal": None,
            "trend_score": 0.0,
            "atr_15m": 0.0,
        }
