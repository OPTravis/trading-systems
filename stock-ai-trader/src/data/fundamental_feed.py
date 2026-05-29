"""
Fundamental data feed using Financial Modeling Prep API.
Free tier: 250 calls/day.
"""
import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"


class FundamentalFeed:
    """
    Fundamental financial data from Financial Modeling Prep (FMP).
    Provides key metrics, financial ratios, and company profiles.
    """

    def __init__(self, api_key: Optional[str] = None, cache_ttl: int = 3600):
        """
        Args:
            api_key: FMP API key (defaults to FMP_API_KEY env var).
            cache_ttl: Cache TTL in seconds (default 1 hour).
        """
        self.api_key = api_key or os.environ.get("FMP_API_KEY", "")
        if not self.api_key:
            logger.warning("No FMP API key configured – requests will fail")
        self.cache_ttl = cache_ttl
        self._cache: dict[str, Any] = {}

    def _get(self, endpoint: str, params: Optional[dict] = None) -> Any:
        """Make an authenticated GET request to the FMP API."""
        p = params or {}
        p["apikey"] = self.api_key
        url = f"{FMP_BASE_URL}/{endpoint}"
        logger.debug("FMP request: %s", url)
        resp = requests.get(url, params=p, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "Error Message" in data:
            raise ValueError(f"FMP error: {data['Error Message']}")
        return data

    # -- public API ----------------------------------------------------------

    def get_key_metrics(self, symbol: str, period: str = "annual", limit: int = 4) -> list[dict]:
        """
        Get key financial metrics (PE, PB, ROE, EV/EBITDA, etc.).

        Args:
            symbol: Ticker symbol.
            period: 'annual' or 'quarter'.
            limit: Number of periods to return.

        Returns:
            List of metric dicts, most recent first.
        """
        cache_key = f"metrics|{symbol}|{period}|{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching key metrics for %s", symbol)
        data = self._get(f"key-metrics/{symbol}", {"period": period, "limit": limit})
        self._cache[cache_key] = data
        return data

    def get_financial_ratios(self, symbol: str, period: str = "annual", limit: int = 4) -> list[dict]:
        """
        Get financial ratios (profitability, liquidity, leverage, efficiency).

        Returns:
            List of ratio dicts, most recent first.
        """
        cache_key = f"ratios|{symbol}|{period}|{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching financial ratios for %s", symbol)
        data = self._get(f"ratios/{symbol}", {"period": period, "limit": limit})
        self._cache[cache_key] = data
        return data

    def get_company_profile(self, symbol: str) -> dict:
        """
        Get company profile (sector, industry, description, market cap, etc.).

        Returns:
            Dict with company profile information.
        """
        cache_key = f"profile|{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching company profile for %s", symbol)
        data = self._get(f"profile/{symbol}")
        result = data[0] if isinstance(data, list) and data else data
        self._cache[cache_key] = result
        return result

    def get_income_statement(self, symbol: str, period: str = "annual", limit: int = 4) -> list[dict]:
        """Get income statement data."""
        cache_key = f"income|{symbol}|{period}|{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        data = self._get(f"income-statement/{symbol}", {"period": period, "limit": limit})
        self._cache[cache_key] = data
        return data

    def get_balance_sheet(self, symbol: str, period: str = "annual", limit: int = 4) -> list[dict]:
        """Get balance sheet data."""
        cache_key = f"balance|{symbol}|{period}|{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        data = self._get(f"balance-sheet-statement/{symbol}", {"period": period, "limit": limit})
        self._cache[cache_key] = data
        return data

    def get_cash_flow(self, symbol: str, period: str = "annual", limit: int = 4) -> list[dict]:
        """Get cash flow statement data."""
        cache_key = f"cashflow|{symbol}|{period}|{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        data = self._get(f"cash-flow-statement/{symbol}", {"period": period, "limit": limit})
        self._cache[cache_key] = data
        return data
