"""
Corporate Actions - Dividends, splits, and merger handling.

Provides utilities to fetch corporate action data and adjust price/volume data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    DIVIDEND = "DIVIDEND"
    SPLIT = "SPLIT"
    MERGER = "MERGER"
    SPIN_OFF = "SPIN_OFF"


@dataclass
class Dividend:
    """Represents a dividend event."""
    symbol: str
    ex_date: date
    pay_date: Optional[date]
    amount: float  # per share
    currency: str = "USD"
    special: bool = False


@dataclass
class Split:
    """Represents a stock split event."""
    symbol: str
    ex_date: date
    ratio_from: int  # e.g., 1 in a 1:4 split
    ratio_to: int    # e.g., 4 in a 1:4 split

    @property
    def split_factor(self) -> float:
        """Multiplier for share count (4.0 for a 1:4 split)."""
        return self.ratio_to / self.ratio_from

    @property
    def reverse(self) -> bool:
        """True if this is a reverse split (e.g., 4:1)."""
        return self.ratio_from > self.ratio_to


@dataclass
class Merger:
    """Represents a merger/acquisition event."""
    acquirer: str
    target: str
    announce_date: date
    close_date: Optional[date]
    exchange_ratio: Optional[float] = None
    cash_per_share: Optional[float] = None


class CorporateActions:
    """
    Corporate actions manager.

    In production, this would fetch from a data provider (IBKR, Bloomberg, etc.).
    Currently stores data in-memory and provides adjustment utilities.
    """

    def __init__(self) -> None:
        self._dividends: dict[str, list[Dividend]] = {}
        self._splits: dict[str, list[Split]] = {}
        self._mergers: list[Merger] = []

    # ── Data Loading ─────────────────────────────────────────────────────

    def add_dividend(self, dividend: Dividend) -> None:
        """Register a dividend event."""
        self._dividends.setdefault(dividend.symbol, []).append(dividend)

    def add_split(self, split: Split) -> None:
        """Register a split event."""
        self._splits.setdefault(split.symbol, []).append(split)

    def add_merger(self, merger: Merger) -> None:
        """Register a merger event."""
        self._mergers.append(merger)

    # ── Queries ──────────────────────────────────────────────────────────

    def get_dividend_history(
        self,
        symbol: str,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> list[Dividend]:
        """
        Get dividend history for a symbol.

        Args:
            symbol: Stock ticker.
            start: Filter start date (inclusive).
            end: Filter end date (inclusive).

        Returns:
            List of Dividend objects, sorted by ex_date.
        """
        divs = self._dividends.get(symbol.upper(), [])
        if start:
            divs = [d for d in divs if d.ex_date >= start]
        if end:
            divs = [d for d in divs if d.ex_date <= end]
        return sorted(divs, key=lambda d: d.ex_date)

    def get_split_history(
        self,
        symbol: str,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> list[Split]:
        """
        Get split history for a symbol.

        Returns:
            List of Split objects, sorted by ex_date.
        """
        splits = self._splits.get(symbol.upper(), [])
        if start:
            splits = [s for s in splits if s.ex_date >= start]
        if end:
            splits = [s for s in splits if s.ex_date <= end]
        return sorted(splits, key=lambda s: s.ex_date)

    def get_merger(self, symbol: str) -> Optional[Merger]:
        """Get merger info if the symbol was involved in one."""
        symbol = symbol.upper()
        for m in self._mergers:
            if m.acquirer == symbol or m.target == symbol:
                return m
        return None

    # ── Price Adjustments ────────────────────────────────────────────────

    def adjust_for_splits(
        self,
        df: pd.DataFrame,
        symbol: str,
    ) -> pd.DataFrame:
        """
        Adjust OHLCV data for stock splits.

        The DataFrame must have columns: open, high, low, close, volume
        and a DatetimeIndex or a 'date' column.

        Args:
            df: OHLCV DataFrame.
            symbol: Stock ticker.

        Returns:
            Adjusted DataFrame (copy).
        """
        splits = self.get_split_history(symbol)
        if not splits:
            return df.copy()

        result = df.copy()

        # Ensure we have a date index
        if not isinstance(result.index, pd.DatetimeIndex):
            if "date" in result.columns:
                result = result.set_index("date")
            else:
                raise ValueError("DataFrame needs DatetimeIndex or 'date' column")

        result = result.sort_index()

        # Ensure numeric columns are float to avoid int64 cast issues
        numeric_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in result.columns]
        for col in numeric_cols:
            result[col] = result[col].astype(float)

        for split in splits:
            split_date = pd.Timestamp(split.ex_date)
            factor = split.split_factor

            # Prices before ex-date are divided by factor (they were "higher")
            # Volume before ex-date is multiplied by factor
            mask = result.index < split_date
            price_cols = ["open", "high", "low", "close"]
            for col in price_cols:
                if col in result.columns:
                    result.loc[mask, col] = result.loc[mask, col] / factor
            if "volume" in result.columns:
                result.loc[mask, "volume"] = result.loc[mask, "volume"] * factor

        return result

    def adjust_for_dividends(
        self,
        df: pd.DataFrame,
        symbol: str,
    ) -> pd.DataFrame:
        """
        Adjust close prices for dividends (backward adjustment).

        This creates dividend-adjusted prices so that total return
        is reflected in the adjusted close.
        """
        divs = self.get_dividend_history(symbol)
        if not divs:
            return df.copy()

        result = df.copy()
        if not isinstance(result.index, pd.DatetimeIndex):
            if "date" in result.columns:
                result = result.set_index("date")
            else:
                raise ValueError("DataFrame needs DatetimeIndex or 'date' column")

        result = result.sort_index()

        price_cols = [c for c in ["open", "high", "low", "close"] if c in result.columns]

        # Iterate dividends sorted by ex_date DESCENDING (most recent first) for backward adjustment
        for div in sorted(divs, key=lambda d: d.ex_date, reverse=True):
            ex_date = pd.Timestamp(div.ex_date)
            mask = result.index < ex_date
            if mask.any() and "close" in result.columns:
                last_close_before = result.loc[mask, "close"].iloc[-1]
                if last_close_before > 0:
                    adj_factor = 1 - (div.amount / last_close_before)
                    for col in price_cols:
                        result.loc[mask, col] = result.loc[mask, col] * adj_factor

        return result

    def get_full_adjustment(
        self,
        df: pd.DataFrame,
        symbol: str,
    ) -> pd.DataFrame:
        """Apply both split and dividend adjustments."""
        result = self.adjust_for_splits(df, symbol)
        result = self.adjust_for_dividends(result, symbol)
        return result
