"""
Earnings calendar and history using Alpha Vantage API.
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

AV_BASE_URL = "https://www.alphavantage.co/query"


class EarningsCalendar:
    """
    Earnings calendar data from Alpha Vantage.
    Provides upcoming earnings, historical earnings, and earnings-day checks.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        if not self.api_key:
            logger.warning("No Alpha Vantage API key configured")
        self._cache: dict = {}

    def _query(self, function: str, params: Optional[dict] = None) -> dict | list:
        p = {"function": function, "apikey": self.api_key}
        if params:
            p.update(params)
        resp = requests.get(AV_BASE_URL, params=p, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_upcoming_earnings(self, days_ahead: int = 30) -> list[dict]:
        """
        Get upcoming earnings events within the next N days.

        Returns:
            List of dicts with keys: symbol, name, report_date, fiscal_date_ending,
            estimate.
        """
        cache_key = f"upcoming|{days_ahead}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching upcoming earnings (next %d days)", days_ahead)
        data = self._query("EARNINGS_CALENDAR")
        # Alpha Vantage returns a CSV-like JSON; handle both formats
        if isinstance(data, dict):
            items = data.get("data", data.get("earnings", []))
        else:
            items = data

        today = date.today()
        cutoff = today + timedelta(days=days_ahead)
        upcoming = []
        for item in items:
            try:
                report_str = item.get("reportDate", item.get("report_date", ""))
                if not report_str:
                    continue
                report_date = datetime.strptime(report_str, "%Y-%m-%d").date()
                if today <= report_date <= cutoff:
                    upcoming.append(
                        {
                            "symbol": item.get("symbol", ""),
                            "name": item.get("name", ""),
                            "report_date": report_str,
                            "fiscal_date_ending": item.get("fiscalDateEnding", ""),
                            "estimate": float(item.get("epsEstimate", 0) or 0),
                        }
                    )
            except (ValueError, TypeError):
                continue

        self._cache[cache_key] = upcoming
        return upcoming

    def get_earnings_history(self, symbol: str) -> list[dict]:
        """
        Get historical earnings for a symbol.

        Returns:
            List of dicts with keys: fiscal_date_ending, reported_eps, estimated_eps,
            surprise, surprise_percentage, report_date.
        """
        cache_key = f"history|{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching earnings history for %s", symbol)
        data = self._query("EARNINGS", {"symbol": symbol})
        if isinstance(data, list):
            raw = data
        else:
            raw = data.get("quarterlyEarnings", data.get("earnings", []))
        history = []
        for item in raw:
            try:
                history.append(
                    {
                        "fiscal_date_ending": item.get("fiscalDateEnding", ""),
                        "reported_eps": float(item.get("reportedEPS", 0) or 0),
                        "estimated_eps": float(item.get("estimatedEPS", 0) or 0),
                        "surprise": float(item.get("surprise", 0) or 0),
                        "surprise_pct": float(item.get("surprisePercentage", 0) or 0),
                        "report_date": item.get("reportedDate", ""),
                    }
                )
            except (ValueError, TypeError):
                continue

        self._cache[cache_key] = history
        return history

    def is_earnings_day(self, symbol: str, target_date: Optional[date] = None) -> bool:
        """
        Check whether a given date is an earnings report day for the symbol.

        Args:
            symbol: Ticker symbol.
            target_date: Date to check (defaults to today).

        Returns:
            True if the symbol reports earnings on target_date.
        """
        target = target_date or date.today()
        target_str = target.strftime("%Y-%m-%d")
        symbol.upper()

        # Check historical earnings for this specific symbol first (cheaper)
        history = self.get_earnings_history(symbol)
        for item in history:
            try:
                if item["report_date"][:10] == target_str:
                    return True
            except (KeyError, TypeError):
                continue

        # Check upcoming earnings filtered to this symbol
        cache_key = f"upcoming_symbol|{symbol}|{target_str}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            data = self._query("EARNINGS_CALENDAR", {"symbol": symbol})
            if isinstance(data, dict):
                items = data.get("data", data.get("earnings", []))
            else:
                items = data

            found = False
            for item in items:
                try:
                    report_str = item.get("reportDate", item.get("report_date", ""))[
                        :10
                    ]
                    if report_str == target_str:
                        found = True
                        break
                except (TypeError, AttributeError):
                    continue

            self._cache[cache_key] = found
            return found
        except Exception as exc:
            logger.debug(
                "Symbol-filtered earnings lookup failed for %s: %s", symbol, exc
            )
            return False
