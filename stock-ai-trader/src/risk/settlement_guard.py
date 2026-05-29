"""
Settlement Guard - T+1 (US) / T+2 (HK) settlement tracking.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List
from dataclasses import dataclass, field

import pandas as pd
from pandas.tseries.offsets import BDay

logger = logging.getLogger(__name__)

SETTLEMENT_DAYS = {
    'US': 1,  # T+1
    'HK': 2,  # T+2
    'DEFAULT': 2,
}


@dataclass
class UnsettledSale:
    """Record of an unsettled sale."""
    amount: float
    market: str
    sale_date: date
    settle_date: date


@dataclass
class SettlementGuard:
    """Tracks unsettled funds from sales."""
    unsettled: List[UnsettledSale] = field(default_factory=list)
    total_cash: float = 0.0

    def get_available_cash(self, today: date = None) -> float:
        """Get cash available for trading (excludes unsettled funds)."""
        today = today or date.today()
        self._settle_past_sales(today)
        unsettled_total = sum(s.amount for s in self.unsettled)
        if self.total_cash < 0:
            logger.warning(f"total_cash is negative: ${self.total_cash:.2f}")
        return max(0.0, self.total_cash - unsettled_total)

    def record_sale(self, amount: float, market: str = 'US', sale_date: date = None):
        """Record a sale that needs to settle."""
        sale_date = sale_date or date.today()
        settle_days = SETTLEMENT_DAYS.get(market.upper(), SETTLEMENT_DAYS['DEFAULT'])
        settle_date = (pd.Timestamp(sale_date) + BDay(settle_days)).date()

        sale = UnsettledSale(
            amount=amount,
            market=market.upper(),
            sale_date=sale_date,
            settle_date=settle_date,
        )
        self.unsettled.append(sale)
        self.total_cash += amount
        logger.info(f"Sale recorded: ${amount:.2f} ({market}), settles {settle_date}")

    def record_purchase(self, amount: float):
        """Record a cash purchase."""
        self.total_cash -= amount

    def set_cash(self, amount: float):
        """Set total cash balance."""
        self.total_cash = amount

    def _settle_past_sales(self, today: date):
        """Remove sales that have settled."""
        before = len(self.unsettled)
        self.unsettled = [s for s in self.unsettled if s.settle_date > today]
        settled = before - len(self.unsettled)
        if settled:
            logger.info(f"Settled {settled} sales")

    def get_unsettle_breakdown(self, today: date = None) -> Dict[str, float]:
        """Get unsettled amounts by market."""
        today = today or date.today()
        self._settle_past_sales(today)
        breakdown = {}
        for s in self.unsettled:
            breakdown[s.market] = breakdown.get(s.market, 0) + s.amount
        return breakdown
