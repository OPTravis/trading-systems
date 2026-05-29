"""
Market Calendar - Holiday and trading day management for US, HK, CN markets.

Provides holiday lists for 2026 and utilities to check trading days.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from .market_hours import Market

logger = logging.getLogger(__name__)

# ─── Holiday Definitions 2026 ───────────────────────────────────────────────

US_HOLIDAYS_2026: list[date] = [
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # Martin Luther King Jr. Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 7, 3),   # Independence Day (observed, July 4 is Saturday)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving Day
    date(2026, 12, 25), # Christmas Day
]

HK_HOLIDAYS_2026: list[date] = [
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 29),  # Lunar New Year
    date(2026, 1, 30),  # Lunar New Year
    date(2026, 1, 31),  # Lunar New Year
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 4),   # Ching Ming Festival (Saturday, observed)
    date(2026, 4, 6),   # Easter Monday
    date(2026, 5, 1),   # Labour Day
    date(2026, 5, 24),  # Buddha's Birthday
    date(2026, 6, 19),  # Tuen Ng Festival
    date(2026, 7, 1),   # HKSAR Establishment Day
    date(2026, 9, 25),  # Mid-Autumn Festival
    date(2026, 10, 1),  # National Day
    date(2026, 10, 29), # Chung Yeung Festival
    date(2026, 12, 25), # Christmas Day
    date(2026, 12, 26), # Boxing Day (if not Sunday)
]

CN_HOLIDAYS_2026: list[date] = [
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 2),   # New Year's Day (extended)
    date(2026, 1, 29),  # Spring Festival
    date(2026, 1, 30),  # Spring Festival
    date(2026, 1, 31),  # Spring Festival
    date(2026, 2, 1),   # Spring Festival
    date(2026, 2, 2),   # Spring Festival
    date(2026, 4, 5),   # Qingming Festival
    date(2026, 4, 6),   # Qingming Festival (extended)
    date(2026, 5, 1),   # Labour Day
    date(2026, 5, 2),   # Labour Day (extended)
    date(2026, 5, 3),   # Labour Day (extended)
    date(2026, 6, 19),  # Dragon Boat Festival
    date(2026, 10, 1),  # National Day
    date(2026, 10, 2),  # National Day
    date(2026, 10, 3),  # National Day
    date(2026, 10, 4),  # National Day (extended)
    date(2026, 10, 5),  # National Day (extended)
    date(2026, 10, 6),  # Mid-Autumn Festival
]

_HOLIDAYS: dict[Market, list[date]] = {
    Market.US: US_HOLIDAYS_2026,
    Market.HK: HK_HOLIDAYS_2026,
    Market.CN: CN_HOLIDAYS_2026,
}


class MarketCalendar:
    """Trading calendar with holiday awareness."""

    def __init__(self, year: int = 2026) -> None:
        self._year = year
        self._holidays: dict[Market, list[date]] = {
            Market.US: [h for h in US_HOLIDAYS_2026 if h.year == year],
            Market.HK: [h for h in HK_HOLIDAYS_2026 if h.year == year],
            Market.CN: [h for h in CN_HOLIDAYS_2026 if h.year == year],
        }
        # Allow adding custom holidays per market
        self._extra_holidays: dict[Market, set[date]] = {
            m: set() for m in Market
        }

    def add_holiday(self, market: str | Market, holiday_date: date) -> None:
        """Add a custom holiday for a market."""
        market = Market(market) if isinstance(market, str) else market
        self._extra_holidays[market].add(holiday_date)

    def is_holiday(self, d: date, market: str | Market = Market.US) -> bool:
        """Check if a date is a market holiday."""
        market = Market(market) if isinstance(market, str) else market
        official = self._holidays.get(market, [])
        extra = self._extra_holidays.get(market, set())
        return d in official or d in extra

    def is_trading_day(self, d: date, market: str | Market = Market.US) -> bool:
        """
        Check if a date is a trading day (weekday and not a holiday).
        """
        if d.weekday() >= 5:  # Saturday or Sunday
            return False
        if self.is_holiday(d, market):
            return False
        return True

    def get_trading_days(
        self,
        start: date | datetime,
        end: date | datetime,
        market: str | Market = Market.US,
    ) -> list[date]:
        """
        Get all trading days between start and end (inclusive).
        """
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()

        market = Market(market) if isinstance(market, str) else market
        trading_days = []
        current = start
        while current <= end:
            if self.is_trading_day(current, market):
                trading_days.append(current)
            current += timedelta(days=1)
        return trading_days

    def next_trading_day(
        self, d: date, market: str | Market = Market.US
    ) -> date:
        """Get the next trading day after the given date."""
        market = Market(market) if isinstance(market, str) else market
        next_day = d + timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(next_day, market):
                return next_day
            next_day += timedelta(days=1)
        return next_day  # best effort after 30 iterations

    def previous_trading_day(
        self, d: date, market: str | Market = Market.US
    ) -> date:
        """Get the previous trading day before the given date."""
        market = Market(market) if isinstance(market, str) else market
        prev_day = d - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(prev_day, market):
                return prev_day
            prev_day -= timedelta(days=1)
        return prev_day  # best effort after 30 iterations

    def trading_days_between(
        self,
        start: date | datetime,
        end: date | datetime,
        market: str | Market = Market.US,
    ) -> int:
        """Count trading days between start and end (inclusive)."""
        return len(self.get_trading_days(start, end, market))

    def get_holidays(
        self, year: Optional[int] = None, market: str | Market = Market.US
    ) -> list[date]:
        """Get all holidays for a market in a given year."""
        market = Market(market) if isinstance(market, str) else market
        official = self._holidays.get(market, [])
        extra = list(self._extra_holidays.get(market, set()))
        all_holidays = sorted(official + extra)
        if year:
            return [h for h in all_holidays if h.year == year]
        return all_holidays
