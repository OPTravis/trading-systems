"""
Market Hours - Trading session management for US, HK, and CN markets.

Handles regular hours, pre-market, after-hours, and timezone conversions.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class Market(str, Enum):
    US = "US"
    HK = "HK"
    CN = "CN"


class MarketState(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    OPEN = "OPEN"
    LUNCH_BREAK = "LUNCH_BREAK"
    POST_MARKET = "POST_MARKET"
    CLOSED = "CLOSED"


# Market session definitions (local times)
_MARKET_SESSIONS: dict[Market, dict] = {
    Market.US: {
        "timezone": "America/New_York",
        "pre_market": (time(4, 0), time(9, 30)),
        "regular": (time(9, 30), time(16, 0)),
        "post_market": (time(16, 0), time(20, 0)),
    },
    Market.HK: {
        "timezone": "Asia/Hong_Kong",
        "pre_market": None,  # No extended hours for HK
        "regular": (time(9, 30), time(16, 0)),
        "lunch_break": (time(12, 0), time(13, 0)),
        "post_market": None,
    },
    Market.CN: {
        "timezone": "Asia/Shanghai",
        "pre_market": None,
        "regular": (time(9, 30), time(15, 0)),
        "lunch_break": (time(11, 30), time(13, 0)),
        "post_market": None,
    },
}


class MarketHours:
    """Manages trading hours and market state for multiple markets."""

    def __init__(self) -> None:
        self._sessions = _MARKET_SESSIONS

    def _now_in_tz(self, market: Market) -> datetime:
        """Get current time in the market's timezone."""
        tz = ZoneInfo(self._sessions[market]["timezone"])
        return datetime.now(tz)

    def is_market_open(self, market: str | Market = Market.US) -> bool:
        """
        Check if the market is currently in a regular trading session.
        Does NOT check holidays - combine with MarketCalendar for that.
        """
        market = Market(market) if isinstance(market, str) else market
        now = self._now_in_tz(market)
        sessions = self._sessions[market]

        regular_start, regular_end = sessions["regular"]

        # Check regular hours
        if not (regular_start <= now.time() < regular_end):
            return False

        # Check lunch break for HK/CN
        if "lunch_break" in sessions and sessions["lunch_break"] is not None:
            lunch_start, lunch_end = sessions["lunch_break"]
            if lunch_start <= now.time() < lunch_end:
                return False

        return True

    def get_market_state(self, market: str | Market = Market.US) -> str:
        """
        Get the current market state.

        Returns:
            One of: PRE_MARKET, OPEN, LUNCH_BREAK, POST_MARKET, CLOSED
        """
        market = Market(market) if isinstance(market, str) else market
        now = self._now_in_tz(market)
        sessions = self._sessions[market]
        t = now.time()

        regular_start, regular_end = sessions["regular"]

        # Pre-market (US only)
        if sessions.get("pre_market"):
            pm_start, pm_end = sessions["pre_market"]
            if pm_start <= t < pm_end:
                return MarketState.PRE_MARKET.value

        # Regular session
        if regular_start <= t < regular_end:
            # Check lunch break for HK/CN
            if "lunch_break" in sessions and sessions["lunch_break"] is not None:
                lunch_start, lunch_end = sessions["lunch_break"]
                if lunch_start <= t < lunch_end:
                    return MarketState.LUNCH_BREAK.value
            return MarketState.OPEN.value

        # Post-market (US only)
        if sessions.get("post_market"):
            post_start, post_end = sessions["post_market"]
            if post_start <= t < post_end:
                return MarketState.POST_MARKET.value

        return MarketState.CLOSED.value

    def next_market_open(self, market: str | Market = Market.US) -> datetime:
        """
        Get the datetime of the next market open.
        Skips weekends (but NOT holidays - use MarketCalendar for that).
        """
        market = Market(market) if isinstance(market, str) else market
        sessions = self._sessions[market]
        tz = ZoneInfo(sessions["timezone"])
        now = datetime.now(tz)
        regular_start, _ = sessions["regular"]

        # If before today's open, next open is today
        if now.time() < regular_start and now.weekday() < 5:
            return now.replace(
                hour=regular_start.hour,
                minute=regular_start.minute,
                second=0,
                microsecond=0,
            )

        # Otherwise, find next weekday
        next_day = now + timedelta(days=1)
        while next_day.weekday() >= 5:  # Skip weekends
            next_day += timedelta(days=1)

        return next_day.replace(
            hour=regular_start.hour,
            minute=regular_start.minute,
            second=0,
            microsecond=0,
        )

    def next_market_close(self, market: str | Market = Market.US) -> datetime:
        """Get the datetime of the next market close."""
        market = Market(market) if isinstance(market, str) else market
        sessions = self._sessions[market]
        tz = ZoneInfo(sessions["timezone"])
        now = datetime.now(tz)
        _, regular_end = sessions["regular"]

        if now.time() < regular_end and now.weekday() < 5:
            return now.replace(
                hour=regular_end.hour,
                minute=regular_end.minute,
                second=0,
                microsecond=0,
            )

        next_day = now + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)

        return next_day.replace(
            hour=regular_end.hour,
            minute=regular_end.minute,
            second=0,
            microsecond=0,
        )

    def minutes_until_open(self, market: str | Market = Market.US) -> int:
        """Get minutes until the next market open. Returns 0 if already open."""
        if self.is_market_open(market):
            return 0
        next_open = self.next_market_open(market)
        diff = next_open - datetime.now(next_open.tzinfo)
        return max(0, int(diff.total_seconds() / 60))

    def get_sessions(self, market: str | Market = Market.US) -> dict:
        """Return the session definitions for a market."""
        market = Market(market) if isinstance(market, str) else market
        return dict(self._sessions[market])
