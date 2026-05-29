"""
PDT Guard - Pattern Day Trader rule enforcement.
Tracks 5-day rolling day trade count for accounts under $25K.
"""
import logging
from datetime import datetime, timedelta
from typing import List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PDT_THRESHOLD = 25000.0  # Minimum account value to avoid PDT restrictions
MAX_DAY_TRADES = 3  # Max day trades in 5 business days for sub-$25K accounts
ROLLING_WINDOW_DAYS = 5  # Business days


@dataclass
class PDTGuard:
    """Pattern Day Trader rule enforcement."""
    day_trades: List[datetime] = field(default_factory=list)

    def can_day_trade(self, account_value: float) -> bool:
        """Check if account can execute a day trade."""
        if account_value >= PDT_THRESHOLD:
            return True
        return self._count_day_trades() < MAX_DAY_TRADES

    def record_day_trade(self, timestamp: datetime = None):
        """Record a day trade execution."""
        ts = timestamp or datetime.now()
        self.day_trades.append(ts)
        logger.info(f"Day trade recorded at {ts}. Rolling count: {self._count_day_trades()}")

    def _count_day_trades(self) -> int:
        """Count day trades in rolling 5-business-day window."""
        cutoff = datetime.now() - timedelta(days=ROLLING_WINDOW_DAYS * 2)  # safe upper bound covering weekends + holidays
        self.day_trades = [t for t in self.day_trades if t > cutoff]
        return len(self.day_trades)

    def get_remaining_day_trades(self, account_value: float) -> int:
        """Get number of remaining day trades allowed."""
        if account_value >= PDT_THRESHOLD:
            return float('inf')
        return max(0, MAX_DAY_TRADES - self._count_day_trades())

    def reset(self):
        """Reset day trade tracking."""
        self.day_trades.clear()
