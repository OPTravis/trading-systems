"""
OHLCV stock data feed from Yahoo Finance and IBKR.
Includes caching with configurable TTL.
"""

import logging
import time
from typing import Any, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class CacheEntry:
    """Simple cache entry with TTL."""

    def __init__(self, data: Any, ttl_seconds: int):
        self.data = data
        self.expiry = time.time() + ttl_seconds

    def is_valid(self) -> bool:
        return time.time() < self.expiry


class StockDataFeed:
    """
    Stock market data feed using Yahoo Finance (primary) and IBKR (optional).
    Provides historical OHLCV data, real-time quotes, and batch quotes
    with a built-in cache layer.
    """

    def __init__(self, ibkr_client=None, default_cache_ttl: int = 300):
        """
        Args:
            ibkr_client: Optional IBKR client instance for real-time data.
            default_cache_ttl: Cache time-to-live in seconds (default 5 min).
        """
        self.ibkr = ibkr_client
        self.default_cache_ttl = default_cache_ttl
        self._cache: dict[str, CacheEntry] = {}

    # -- cache helpers -------------------------------------------------------

    def _cache_key(self, *parts: str) -> str:
        return "|".join(str(p) for p in parts)

    def _get_cached(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry and entry.is_valid():
            return entry.data
        # Evict expired entry
        if entry is not None:
            del self._cache[key]
        return None

    def _set_cached(self, key: str, data: Any, ttl: Optional[int] = None) -> None:
        self._cache[key] = CacheEntry(data, ttl or self.default_cache_ttl)

    def _evict_expired(self) -> None:
        """Remove all expired entries from the in-memory cache."""
        now = time.time()
        expired = [k for k, v in self._cache.items() if now >= v.expiry]
        for k in expired:
            del self._cache[k]

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- helpers for global markets ------------------------------------------

    _NON_US_SUFFIXES = (
        ".HK",
        ".T",
        ".L",
        ".SS",
        ".SZ",
        ".DE",
        ".PA",
        ".AS",
        ".SW",
        ".AX",
        ".MI",
        ".MC",
        ".CO",
        ".OL",
        ".ST",
        ".HE",
        ".BR",
        ".LS",
        ".VI",
    )

    @classmethod
    def _is_non_us_ticker(cls, symbol: str) -> bool:
        """Check if a ticker has a non-US suffix (HK, JP, EU, AU, etc.)."""
        upper = symbol.upper()
        return any(upper.endswith(s) for s in cls._NON_US_SUFFIXES)

    # -- public API ----------------------------------------------------------

    # Alias for compatibility with stock_researcher which calls get_history
    def get_history(self, *args, **kwargs):
        return self.get_historical(*args, **kwargs)

    def get_historical(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        Get historical OHLCV data.

        Args:
            symbol: Ticker symbol (e.g. 'AAPL').
            period: Data period ('1d','5d','1mo','3mo','6mo','1y','2y','5y','max').
            interval: Bar interval ('1m','2m','5m','15m','30m','60m','90m','1h','1d','5d','1wk','1mo','3mo').

        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume (index=Datetime).
        """
        cache_key = self._cache_key("hist", symbol, period, interval)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        logger.info("Fetching historical data: %s (%s, %s)", symbol, period, interval)
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            logger.warning("No historical data returned for %s", symbol)
        else:
            # Normalize column names to lowercase (yfinance returns capitalized)
            df.columns = [c.lower() for c in df.columns]

        self._set_cached(cache_key, df)
        return df

    def get_realtime_quote(self, symbol: str) -> dict:
        """
        Get a real-time (or near-real-time) quote.

        Returns:
            Dict with keys: symbol, price, change, change_pct, volume, bid, ask,
                            day_high, day_low, market_cap, timestamp.
        """
        cache_key = self._cache_key("quote", symbol)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # Try IBKR first for true real-time (US tickers only)
        if self.ibkr is not None and not self._is_non_us_ticker(symbol):
            try:
                quote = self.ibkr.get_market_data(symbol)
                self._set_cached(cache_key, quote, ttl=10)
                return quote
            except Exception:
                logger.debug(
                    "IBKR quote failed for %s, falling back to yfinance", symbol
                )

        # Yahoo Finance fallback
        logger.info("Fetching realtime quote for %s via yfinance", symbol)
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        quote = {
            "symbol": symbol,
            "price": float(getattr(info, "last_price", 0) or 0),
            "change": float(getattr(info, "regular_market_change", 0) or 0),
            "change_pct": float(getattr(info, "regular_market_change_percent", 0) or 0),
            "volume": int(getattr(info, "last_volume", 0) or 0),
            "bid": float(getattr(info, "bid", 0) or 0),
            "ask": float(getattr(info, "ask", 0) or 0),
            "day_high": float(getattr(info, "day_high", 0) or 0),
            "day_low": float(getattr(info, "day_low", 0) or 0),
            "market_cap": float(getattr(info, "market_cap", 0) or 0),
            "timestamp": pd.Timestamp.now().isoformat(),
        }
        self._set_cached(cache_key, quote, ttl=15)
        return quote

    def get_multiple_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """
        Get real-time quotes for multiple symbols in one call.

        Args:
            symbols: List of ticker symbols.

        Returns:
            Dict mapping symbol -> quote dict.
        """
        results: dict[str, dict] = {}
        uncached: list[str] = []

        for sym in symbols:
            cache_key = self._cache_key("quote", sym)
            cached = self._get_cached(cache_key)
            if cached is not None:
                results[sym] = cached
            else:
                uncached.append(sym)

        if not uncached:
            return results

        logger.info("Fetching batch quotes for %d symbols via yfinance", len(uncached))
        tickers = yf.Tickers(" ".join(uncached))
        for sym in uncached:
            try:
                info = tickers.tickers[sym].fast_info
                quote = {
                    "symbol": sym,
                    "price": float(getattr(info, "last_price", 0) or 0),
                    "change": float(getattr(info, "regular_market_change", 0) or 0),
                    "change_pct": float(
                        getattr(info, "regular_market_change_percent", 0) or 0
                    ),
                    "volume": int(getattr(info, "last_volume", 0) or 0),
                    "market_cap": float(getattr(info, "market_cap", 0) or 0),
                    "timestamp": pd.Timestamp.now().isoformat(),
                }
                self._set_cached(self._cache_key("quote", sym), quote, ttl=15)
                results[sym] = quote
            except Exception as exc:
                logger.error("Failed to get quote for %s: %s", sym, exc)
                results[sym] = {"symbol": sym, "error": str(exc)}

        return results
