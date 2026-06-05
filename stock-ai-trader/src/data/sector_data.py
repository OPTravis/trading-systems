"""
Sector performance and rotation signals via FMP and Yahoo Finance.
"""

import logging

logger = logging.getLogger(__name__)

# SPDR sector ETFs for benchmarking
SECTOR_ETFS = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}


def _get_yfinance():
    """Lazy-import yfinance; raises clear error if not installed."""
    try:
        import yfinance as yf
        return yf
    except ImportError:
        raise ImportError(
            "yfinance is required for sector data. Install with: pip install yfinance"
        )


class SectorData:
    """
    Sector-level performance and rotation analysis.
    Uses SPDR sector ETFs as proxies.
    """

    def __init__(self):
        self._cache: dict = {}

    def get_sector_performance(self, period: str = "1mo") -> dict:
        """
        Get performance of all 11 GICS sectors over a given period.

        Args:
            period: Look-back period ('1d','5d','1mo','3mo','6mo','1y').

        Returns:
            Dict mapping sector name -> performance dict with keys:
            symbol, return_pct, current_price, volume.
        """
        cache_key = f"sector_perf|{period}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        yf = _get_yfinance()

        logger.info("Fetching sector performance (%s)", period)
        results = {}
        tickers_str = " ".join(SECTOR_ETFS.values())
        tickers = yf.Tickers(tickers_str)

        for sector, etf in SECTOR_ETFS.items():
            try:
                hist = tickers.tickers[etf].history(period=period)
                if hist.empty or len(hist) < 2:
                    continue
                start_price = hist["Close"].iloc[0]
                end_price = hist["Close"].iloc[-1]
                ret = ((end_price - start_price) / start_price) * 100
                results[sector] = {
                    "symbol": etf,
                    "return_pct": round(float(ret), 2),
                    "current_price": round(float(end_price), 2),
                    "volume": int(hist["Volume"].iloc[-1]),
                }
            except Exception as exc:
                logger.error("Sector data error for %s (%s): %s", sector, etf, exc)

        self._cache[cache_key] = results
        return results

    def get_sector_rotation_signals(self) -> dict:
        """
        Analyze sector rotation using relative strength across multiple periods.

        Returns:
            Dict with keys:
            - momentum_leaders: sectors with strongest recent momentum
            - momentum_laggards: weakest sectors
            - rotation_signal: 'risk_on', 'risk_off', or 'neutral'
        """
        logger.info("Computing sector rotation signals")

        perf_1m = self.get_sector_performance("1mo")
        perf_3m = self.get_sector_performance("3mo")

        if not perf_1m or not perf_3m:
            return {
                "momentum_leaders": [],
                "momentum_laggards": [],
                "rotation_signal": "neutral",
            }

        # Composite score: 60% 1-month + 40% 3-month
        scores = {}
        for sector in perf_1m:
            if sector in perf_3m:
                s1 = perf_1m[sector]["return_pct"]
                s3 = perf_3m[sector]["return_pct"]
                scores[sector] = 0.6 * s1 + 0.4 * s3

        sorted_sectors = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        leaders = [s[0] for s in sorted_sectors[:3]]
        laggards = [s[0] for s in sorted_sectors[-3:]]

        # Risk-on: cyclicals (Tech, Consumer Disc., Industrials) leading
        # Risk-off: defensives (Utilities, Staples, Healthcare) leading
        cyclical = {
            "Technology",
            "Consumer Discretionary",
            "Industrials",
            "Financials",
            "Materials",
            "Energy",
        }
        defensive = {"Utilities", "Consumer Staples", "Healthcare", "Real Estate"}

        leader_set = set(leaders)
        cyclical_count = len(leader_set & cyclical)
        defensive_count = len(leader_set & defensive)

        if cyclical_count >= 2:
            signal = "risk_on"
        elif defensive_count >= 2:
            signal = "risk_off"
        else:
            signal = "neutral"

        return {
            "momentum_leaders": leaders,
            "momentum_laggards": laggards,
            "rotation_signal": signal,
            "scores": dict(sorted_sectors),
        }
