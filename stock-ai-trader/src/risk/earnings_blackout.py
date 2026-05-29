"""
Earnings Blackout - Blocks new positions around earnings dates.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

BLACKOUT_DAYS_BEFORE = 2  # Block new positions 2 days before earnings
BLACKOUT_DAYS_AFTER = 0  # Block on earnings day (day of)


@dataclass
class EarningsBlackout:
    """Earnings blackout period enforcement."""
    earnings_dates: Dict[str, date] = field(default_factory=dict)
    _cache_timestamp: Optional[datetime] = field(default=None, repr=False)

    def is_blackout(self, symbol: str, today: date = None) -> bool:
        """Check if symbol is in earnings blackout period."""
        today = today or date.today()
        earnings_date = self.get_next_earnings(symbol)

        if earnings_date is None:
            return False

        # Blackout: from (earnings_date - BLACKOUT_DAYS_BEFORE) to earnings_date inclusive
        blackout_start = earnings_date - timedelta(days=BLACKOUT_DAYS_BEFORE)
        return blackout_start <= today <= earnings_date

    def get_next_earnings(self, symbol: str) -> Optional[date]:
        """Get next earnings date for symbol."""
        return self.earnings_dates.get(symbol.upper())

    def set_earnings_date(self, symbol: str, earnings_date: date):
        """Set/update earnings date for a symbol."""
        self.earnings_dates[symbol.upper()] = earnings_date
        logger.info(f"Set earnings date for {symbol}: {earnings_date}")

    def get_blackout_symbols(self, today: date = None) -> list:
        """Get all symbols currently in blackout."""
        today = today or date.today()
        return [s for s in self.earnings_dates if self.is_blackout(s, today)]

    def days_until_earnings(self, symbol: str, today: date = None) -> Optional[int]:
        """Get days until next earnings for symbol."""
        today = today or date.today()
        earnings_date = self.get_next_earnings(symbol)
        if earnings_date is None:
            return None
        delta = (earnings_date - today).days
        return max(0, delta)
